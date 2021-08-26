#!/bin/bash

gunicorn --bind :8000 --workers 4 --worker-class uvicorn.workers.UvicornWorker saleor.asgi:application & sleep 10
nginx
