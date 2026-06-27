"""CDK stack for the World Cup screener.

Provisions exactly what the scheduled, read-only screener needs:
  - S3 bucket for date-partitioned report/grade output (versioned).
  - Secrets Manager secret holding the Odds/News API keys (you fill the value).
  - Lambda (Python 3.12) running ``screener.aws.handler.lambda_handler``.
  - EventBridge Scheduler firing daily at 09:00 America/Chicago (DST-aware).
  - SES sender identity + send permission for the daily email.

IAM is least-privilege for a screener: the Lambda can read the secret, write the
bucket, and send SES email — and nothing else. There is deliberately no trading
or order permission anywhere; this is decision-support only.

NOTE: the Lambda asset is built WITHOUT Docker. Run ``infra/build_lambda.sh``
first — it pip-installs the Linux-targeted dependency wheels plus the screener
source into ``infra/build/lambda/``, which this stack zips as the function code.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ses as ses
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parent.parent
# Pre-built (Docker-free) Lambda package; produced by infra/build_lambda.sh.
LAMBDA_BUILD = Path(__file__).resolve().parent / "build" / "lambda"


class ScreenerStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        email_sender: str,
        email_recipients: str,
        kalshi_series: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if not LAMBDA_BUILD.exists():
            raise FileNotFoundError(
                f"Lambda package not built at {LAMBDA_BUILD}. "
                "Run `bash infra/build_lambda.sh` before `cdk synth`/`cdk deploy`."
            )

        # --- S3 output bucket --------------------------------------------- #
        bucket = s3.Bucket(
            self,
            "OutputBucket",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,  # keep shadow-mode history
        )

        # --- Secrets Manager: API keys (value filled in after deploy) ----- #
        secret = secretsmanager.Secret(
            self,
            "ApiKeys",
            description="Odds/News API keys for the WC screener (JSON: ODDS_API_KEY, NEWS_API_KEY)",
        )

        # --- SES sender identity ------------------------------------------ #
        # Verifying the identity (and recipients, in the SES sandbox) is a manual
        # step; CDK only declares the sender identity.
        ses.EmailIdentity(self, "SenderIdentity", identity=ses.Identity.email(email_sender))

        # --- Lambda ------------------------------------------------------- #
        fn = lambda_.Function(
            self,
            "ScreenerFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="screener.aws.handler.lambda_handler",
            timeout=Duration.minutes(2),
            memory_size=512,
            architecture=lambda_.Architecture.X86_64,  # matches the manylinux wheels
            code=lambda_.Code.from_asset(str(LAMBDA_BUILD)),
            environment={
                "SCREENER_S3_BUCKET": bucket.bucket_name,
                "SCREENER_SECRET_ID": secret.secret_arn,
                "SCREENER_EMAIL_SENDER": email_sender,
                "SCREENER_EMAIL_RECIPIENTS": email_recipients,
                "SCREENER_KALSHI_SERIES": kalshi_series,
                "SCREENER_CACHE_DIR": "/tmp/cache",  # only /tmp is writable in Lambda
                "SCREENER_LOG_JSON": "true",
            },
        )

        # Least-privilege grants: read secret, write bucket, send email.
        secret.grant_read(fn)
        bucket.grant_read_write(fn)
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=["*"],  # SES identity-scoped policies are awkward; tighten if desired
            )
        )

        # --- EventBridge Scheduler: 09:00 America/Chicago daily ----------- #
        scheduler_role = iam.Role(
            self,
            "SchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        fn.grant_invoke(scheduler_role)

        scheduler.CfnSchedule(
            self,
            "DailySchedule",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            schedule_expression="cron(0 9 * * ? *)",
            schedule_expression_timezone="America/Chicago",
            target=scheduler.CfnSchedule.TargetProperty(
                arn=fn.function_arn,
                role_arn=scheduler_role.role_arn,
            ),
        )

        # Handy references for the post-deploy steps.
        CfnOutput(self, "FunctionName", value=fn.function_name)
        CfnOutput(self, "BucketName", value=bucket.bucket_name)
        CfnOutput(self, "SecretArn", value=secret.secret_arn)
