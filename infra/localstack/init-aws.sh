#!/bin/bash
# Runs inside LocalStack when it is ready: creates the SNS topic plus an SQS
# queue subscribed to it, so you can read the "pages" the detector sent:
#
#   awslocal sqs receive-message --queue-url http://localhost:4566/000000000000/alert-inbox
#   awslocal logs tail /log-anomaly-detector/alerts --follow
set -e
awslocal sns create-topic --name log-anomaly-alerts
awslocal sqs create-queue --queue-name alert-inbox
awslocal sns subscribe \
  --topic-arn arn:aws:sns:us-east-1:000000000000:log-anomaly-alerts \
  --protocol sqs \
  --notification-endpoint arn:aws:sqs:us-east-1:000000000000:alert-inbox
awslocal logs create-log-group --log-group-name /log-anomaly-detector/alerts || true
echo "LocalStack AWS resources ready"
