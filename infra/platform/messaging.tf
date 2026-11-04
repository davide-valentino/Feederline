# ---------------------------------------------------------------------------
# EventBridge custom bus: domain events from app/outbox.py (source
# tv.feederline, schemaVersion 1). EVENT_BUS_NAME points here. No rules yet:
# consumers are wired when the app tier/legacy ordering dictates.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_event_bus" "main" {
  name = "tv.feederline"
  tags = merge(local.tags, { Name = "tv.feederline" })
}
