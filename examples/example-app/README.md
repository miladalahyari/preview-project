# Company example application

A Python HTTP server on port 8080 with `/`, `/health` and `/healthz`. It has no
third-party runtime dependencies. `APP_VERSION`, `PORT` and `SOURCE_URL` are optional
configuration values. Responses expose no other environment variables.

```sh
python3 server.py
python3 -m unittest discover -s .
docker build -t example-app:local .
docker run --rm --read-only --cap-drop=ALL -p 8080:8080 example-app:local
```

Copy this directory's contents, including `.gitlab-ci.yml` and `.dockerignore`,
into a separate GitLab project. Edit the fixed trigger target in `.gitlab-ci.yml`,
configure digest-pinned `TEST_PREVIEW_CI_IMAGE` and `TEST_DIND_IMAGE`, and complete
the platform installation before enabling `TEST_DEPLOYMENTS_ENABLED=true`.
Create a fresh `test-demo` tag to request a preview. For early cleanup, run a web
pipeline with `TEST_PREVIEW_ACTION=stop` and the original source pipeline ID in
`TEST_PREVIEW_PIPELINE_ID`.

The tools image contains the trusted build helper, so the source job needs no
infrastructure checkout or central credential. The runner needs TLS-enabled
Docker-in-Docker and access to pull the tools image. See the main repository's
installation guide for the full contract. Replace the sample base-image tag with
a reviewed digest in your own release process. Set your public source URL in the
platform chart before distributing a modified application.

Licensed under the included [GNU AGPL v3](LICENSE).
