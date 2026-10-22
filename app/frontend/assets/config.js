// Local development settings. Terraform overwrites this file on deployment with
// the real API base ("/api" behind CloudFront) and Cognito identifiers.
window.FACAI_CONFIG = {
  apiBase: "http://localhost:8000",
  region: "ap-southeast-1",
  userPoolId: "",
  clientId: "",
  version: "local",
};
