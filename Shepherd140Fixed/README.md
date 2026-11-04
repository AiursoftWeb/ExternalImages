# Shepherd PR #140 fix

Temporary derivative of the official `containrrr/shepherd:latest` image with [PR #140](https://github.com/containrrr/shepherd/pull/140) applied. It avoids replacing healthy services when registry digest resolution fails in the default HTTPS mode. Explicit no-resolve and insecure-registry modes retain upstream behavior.

All upstream configuration and the entrypoint are inherited. The local patch is applied during the build; incompatible upstream changes fail the build. Return to the official image once the fix is released and verified.
