"""AWS integration: S3 persistence, SES email, Secrets Manager, Lambda handler.

These modules take INJECTED boto3 clients (anything with the right method shape),
so the logic is unit-tested with fakes and never needs real AWS in CI. boto3 is
only imported lazily inside the Lambda entrypoint's wiring, so importing the rest
of the package (local runs, tests) does not require boto3 to be installed.
"""
