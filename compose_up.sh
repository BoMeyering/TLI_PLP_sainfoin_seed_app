#!/bin/bash
#
# Script to deploy the SeedView stack using Docker Compose.
# Assumes .env.docker is properly configured.
#
docker compose --env-file .env.docker up -d