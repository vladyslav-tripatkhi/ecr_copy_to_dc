#!/usr/bin/python3
import os
import docker
import boto3
import base64
import logging

from re import match
from botocore.exceptions import ClientError 

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)


SOURCE_REGION="us-east-1"
DESTINATION_REGIONS=["us-west-1", "eu-west-1"]

def get_ecr_credentials(ecr_client):
  auth_info = ecr_client.get_authorization_token()["authorizationData"][0]
  password = base64.b64decode(auth_info["authorizationToken"]).decode("utf-8").split(":")[-1]
  return dict(username="AWS", password=password)

def describe_repositories(ecr_client, name_regex=None):
  repositories = ecr_client.describe_repositories().get("repositories", [])
  if name_regex and repositories:
    filtered_repositories = []
    for repo in repositories:
      if match(name_regex, repo["repositoryName"]):
        filtered_repositories.append(repo)
    return filtered_repositories
  return repositories
 
# Repo object: repo_name, source_repo_uri, tag_list
def pull_images_from_repo(docker_client, repo_uri, auth_config, skip_pull=False):
    image_tags = []
    repo_name = repo_uri.split("/")[-1]
    # Rename to tagged_images!
    tagged_images = filter(lambda image: 'imageTag' in image,  ecr_client.list_images(repositoryName=repo_name).get("imageIds", []))
    for image in tagged_images:
      image_tag = image['imageTag']
      image_tags.append(image_tag)
      if not skip_pull:
        log.info(f"Pulling docker image {repo_uri}:{image_tag}")
        docker_client.images.pull(repo_uri, tag=image_tag, auth_config=auth_config)
        log.info(f"Successfully pulled docker image {repo_uri}:{image_tag}")
      else:
        log.info(f"Skipped pulling image {repo_uri}:{image_tag}")
    return image_tags

def push_images_to_dest_repo(docker_client, src_repo_info, dest_repo_uri, auth_config, dest_repo_tags=[], skip_push=False):
  for image_tag in src_repo_info["imageTags"]:
    if image_tag in dest_repo_tags:
      log.info(f"Image with tag {image_tag} is present in {dest_repo_uri}. Skipping the push and moving on")
      continue
    src_image_tag = f"{src_repo_info['repositoryUri']}:{image_tag}"
    dest_image_tag = f"{dest_repo_uri}:{image_tag}"
    log.info(f"Tagging {src_image_tag} as {dest_image_tag} for deployment to {dest_repo_uri.split('.')[3]}")
    docker_client.images.get(src_image_tag).tag(dest_image_tag)
    if not skip_push:
      log.info(f"Pushing docker image {dest_repo_uri}:{image_tag}")
      docker_client.images.push(dest_repo_uri, tag=image_tag, auth_config=auth_config)
      log.info(f"Successfully pushed docker image {dest_repo_uri}:{image_tag}")
    else:
      log.info(f"Skipped pushing image {repo_uri}:{image_tag}")
    log.info(f"Removing {dest_image_tag} tag from the host system")
    docker_client.images.remove(dest_image_tag)

def create_ecr_repo(ecr_client, repo_name):
  try:
    return dict( 
      repository=ecr_client.describe_repositories(repositoryNames=[repo_name])["repositories"][0]
    )
  except ClientError as e:
    if e.response["Error"]["Code"] == "RepositoryNotFoundException":
      log.info(f"Repository {repo_name} is not found in {ecr_client.meta.region_name}. Creating it.")
      return ecr_client.create_repository(repositoryName=repo_name)

if __name__ == "__main__":
  session = boto3.Session( profile_name="production_42")
  ecr_client = session.client("ecr", region_name=SOURCE_REGION)

  auth_config = get_ecr_credentials(ecr_client) 
  docker_client = docker.from_env()
  repositories = describe_repositories(ecr_client, ".*wix-bi-mlflow.*")

  repo_list = []

  for repo in repositories:
    repo_uri = repo["repositoryUri"]
    image_tags = pull_images_from_repo(docker_client, repo_uri, auth_config)
    repo_list.append(dict(
      repositoryName=repo["repositoryName"],
      repositoryUri=repo_uri,
      imageTags=image_tags
    ))
  log.info(repo_list)
  for dest_region in DESTINATION_REGIONS:
    dest_ecr_client = session.client("ecr", region_name=dest_region)
    dest_repo_info = create_ecr_repo(dest_ecr_client, repo_list[0]["repositoryName"])["repository"]
    dest_repo_uri = dest_repo_info["repositoryUri"]

    dest_image_tags = []
    for image_info in dest_ecr_client.list_images(repositoryName=dest_repo_info["repositoryName"])["imageIds"]:
      if "imageTag" in image_info:
        dest_image_tags.append(image_info["imageTag"])

    dest_auth_config = get_ecr_credentials(dest_ecr_client)
    for repo_info in repo_list:
      push_images_to_dest_repo(docker_client, repo_info, dest_repo_uri, dest_auth_config, dest_image_tags)
