variable "project" {
  default = "opera"
}

variable "venue" {
  default = "ci"
}

variable "profile" {
  #default = "saml"
  #int
  default = "saml-pub"
}

variable "verdi_release" {
  #default = "v4.0.1-beta.8"
}

variable "registry_release" {
  default = "2"
}

variable "logstash_release" {
  default = "7.9.3"
}

variable "pge_snapshots_date" {
  default = "20210805-R2.0.0"
}

variable "pge_release" {
  default = "R2.0.0"
}

variable "private_key_file" {
  #int
  #default="~/.ssh/operasds-int-cluster-1.pem"
}

variable "keypair_name" {
  #default = "operasds-int-cluster-1"
}

variable "shared_credentials_file" {
  default = "~/.aws/credentials"
}

variable "az" {
  default = "us-west-2a"
}

variable "region" {
  default = "us-west-2"
}

variable "verdi_security_group_id" {
  default = "sg-05c2b46227bb3bf54"
}

variable "pcm_verdi_role" {
  default = {
    name = "am-pcm-dev-verdi-role"
    path = "/"
  }
}

variable "verdi" {
  type = map(string)
  default = {
    name               = "verdi"
    #ami                = "ami-0601c031b967d1e15"  # verdi v4.07 HySDS v4.0.1-beta.2
	ami                = "ami-03a334419b86d28b1"
    instance_type      = "t3.medium"
    device_name        = "/dev/sda1"
    device_size        = 50
    docker_device_name = "/dev/sdf"
    docker_device_size = 25
    private_ip         = ""
    public_ip          = ""
  }
}

variable "asg_use_role" {
  default = "true"
}

variable "asg_vpc" {
  default = "vpc-b5a983cd"
}

variable "subnet_id" {
  default = "subnet-000eb551ad06392c7"
}

variable "artifactory_base_url" {
  default = "https://artifactory-fn.jpl.nasa.gov/artifactory"
}

variable "artifactory_repo" {
  default = "general-develop"
}

variable "artifactory_mirror_url" {
  default = "s3://opera-dev/artifactory_mirror"
}

variable "docker_user" {
  default = ""
}

variable "docker_pwd" {
  default = ""
}

variable "use_s3_uri_structure" {
  default = true
}

variable "artifactory_fn_user" {
  default = ""
}

variable "artifactory_fn_api_key" {
  default = ""
}
