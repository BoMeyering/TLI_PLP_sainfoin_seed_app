#!/bin/bash
#
# Script to shut down the SeedView stack using Docker Compose.
# Assumes .env.docker is properly configured.
#
docker compose --env-file .env.docker down