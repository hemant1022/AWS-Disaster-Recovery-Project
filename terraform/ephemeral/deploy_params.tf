# ---------------------------------------------------------------------------
# Deployment coordinates, published as SSM parameters so the pipeline can find
# the instances without reading Terraform state. The application role is
# allowed to read this prefix and nothing else in the parameter store.
# ---------------------------------------------------------------------------
locals {
  deploy_parameters = {
    primary_region      = var.primary_region
    dr_region           = var.dr_region
    primary_instance_id = aws_instance.primary.id
    dr_instance_id      = aws_instance.dr.id
    primary_eip         = aws_eip.primary.public_ip
    dr_eip              = aws_eip.dr.public_ip
    app_port            = tostring(var.app_port)
    app_fqdn            = "app.${var.domain_name}"
    evidence_bucket     = local.evidence_bucket
  }
}

resource "aws_ssm_parameter" "deploy" {
  for_each = local.deploy_parameters

  name  = "/${var.project}/deploy/${each.key}"
  type  = "String"
  value = each.value
}
