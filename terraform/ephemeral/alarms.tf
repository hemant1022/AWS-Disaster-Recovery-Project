# ---------------------------------------------------------------------------
# Alarm topics are created once in the persistent root. CloudWatch alarm
# actions cannot cross regions, so each region publishes to its own topic and
# both topics share one subscriber.
# ---------------------------------------------------------------------------
data "aws_sns_topic" "alarms_primary" {
  name = "${var.project}-alarms"
}

data "aws_sns_topic" "alarms_dr" {
  provider = aws.dr
  name     = "${var.project}-alarms"
}

# Health-check status lives in us-east-1 whatever region the endpoint is in,
# so these two alarms are created there. This is the detection event the drill
# reads its time-to-detect from.
resource "aws_cloudwatch_metric_alarm" "health_primary" {
  provider = aws.global

  alarm_name          = "${var.project}-primary-endpoint-unhealthy"
  alarm_description   = "Route 53 reports the primary application endpoint unhealthy."
  namespace           = "AWS/Route53"
  metric_name         = "HealthCheckStatus"
  dimensions          = { HealthCheckId = aws_route53_health_check.primary.id }
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  evaluation_periods  = 1
  period              = 60
  statistic           = "Minimum"
  treat_missing_data  = "breaching"

  alarm_actions = [data.aws_sns_topic.alarms_primary.arn]
}

# A Route 53 health-check metric lives in us-east-1, so its alarm must live
# there too -- and CloudWatch alarm actions cannot cross regions, which means
# this one publishes to the us-east-1 topic even though it watches the standby.
resource "aws_cloudwatch_metric_alarm" "health_dr" {
  provider = aws.global

  alarm_name          = "${var.project}-dr-endpoint-unhealthy"
  alarm_description   = "Route 53 reports the standby application endpoint unhealthy."
  namespace           = "AWS/Route53"
  metric_name         = "HealthCheckStatus"
  dimensions          = { HealthCheckId = aws_route53_health_check.dr.id }
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  evaluation_periods  = 1
  period              = 60
  statistic           = "Minimum"
  treat_missing_data  = "breaching"

  alarm_actions = [data.aws_sns_topic.alarms_primary.arn]
}

# Replication lag is the leading indicator of RPO. It is the one metric that
# tells you whether a failover right now would lose data.
resource "aws_cloudwatch_metric_alarm" "replica_lag" {
  provider = aws.dr

  alarm_name          = "${var.project}-replica-lag"
  alarm_description   = "Cross-region replica lag above ${var.replica_lag_alarm_seconds}s."
  namespace           = "AWS/RDS"
  metric_name         = "ReplicaLag"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.replica.identifier }
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.replica_lag_alarm_seconds
  evaluation_periods  = 2
  period              = 60
  statistic           = "Average"
  treat_missing_data  = "notBreaching"

  alarm_actions = [data.aws_sns_topic.alarms_dr.arn]
}

resource "aws_cloudwatch_metric_alarm" "instance_primary" {
  alarm_name          = "${var.project}-primary-instance-status"
  alarm_description   = "Primary application instance failed a status check."
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  dimensions          = { InstanceId = aws_instance.primary.id }
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 2
  period              = 60
  statistic           = "Maximum"
  treat_missing_data  = "notBreaching"

  alarm_actions = [data.aws_sns_topic.alarms_primary.arn]
}

resource "aws_cloudwatch_metric_alarm" "instance_dr" {
  provider = aws.dr

  alarm_name          = "${var.project}-dr-instance-status"
  alarm_description   = "Standby application instance failed a status check."
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  dimensions          = { InstanceId = aws_instance.dr.id }
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 2
  period              = 60
  statistic           = "Maximum"
  treat_missing_data  = "notBreaching"

  alarm_actions = [data.aws_sns_topic.alarms_dr.arn]
}
