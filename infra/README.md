# Deploy (CDK, Python)

Provisions the scheduled screener: S3 output bucket, Secrets Manager secret,
Lambda, a 09:00 America/Chicago EventBridge schedule, and an SES sender identity.

> **Read-only by design.** The Lambda's IAM role can read the secret, write the
> bucket, and send SES email — nothing else. No trading/order permissions exist.

## Prerequisites
- AWS account + credentials configured (`aws configure`).
- Node-based CDK CLI: `npm install -g aws-cdk`.
- **Docker running** (the Lambda asset is bundled by pip-installing the project).
- An email address you control for SES (sender).

## Steps
```bash
cd infra
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# First time per account/region:
cdk bootstrap

cdk deploy \
  -c email_sender=you@example.com \
  -c email_recipients=you@example.com \
  -c kalshi_series=KXWC2026          # optional; else title-keyword discovery
```

## After deploy (manual, one-time)
1. **Fill the secret** (`Wc2026ScreenerStack` → Secrets Manager) with JSON:
   ```json
   {"ODDS_API_KEY": "your-the-odds-api-key", "NEWS_API_KEY": ""}
   ```
2. **Verify SES**: confirm the sender identity email; if your account is in the
   SES sandbox, also verify each recipient (or request production access).
3. **Test once** without waiting for 9am: invoke the Lambda manually
   (`aws lambda invoke --function-name <name> out.json`) and check the S3 bucket
   for `date=<today>/report.json` and your inbox for the email.

## Notes
- The schedule is DST-aware (`schedule_expression_timezone=America/Chicago`).
- Output is retained on stack deletion (`RemovalPolicy.RETAIN`) so shadow-mode
  history survives teardown.
- Grading runs locally against the S3 output (point a future `S3ReportStore` at
  the bucket), or download `report.json` and grade with the local store.
