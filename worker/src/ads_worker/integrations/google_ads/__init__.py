"""Google Ads extractor for batch stage S3a: googleAds:search v25 only via the existing n8n Cloud
webhook ("already built and verified, do not recreate"), one POST per page, never bundled, never
searchStream. Sole credential: GOOGLE_ADS_WEBHOOK_TOKEN, sent as X-Webhook-Token. Read-only Google
access is an accepted dependency on the Google user behind n8n's stored credential, neither enforced
nor claimed verified here (Rule 4). Modules: queries (GAQL templates), contracts (pydantic response
models), client (webhook client, Reader protocol, stage extraction), normalize (rows for load/).
"""
