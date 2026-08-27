# syntax=docker/dockerfile:1
#
# CI runner image: rayspec plus the host tools its bundled workflows expect (git, gh, jq), with
# both agent CLIs already installed so a first `rayspec run` needs no network fetch of them. Not
# a base for extension — see PRD-08's non-goals.
#
# Built from the wheel the release job's `build` job already produced (`docker build
# --build-context` is not used on purpose: the `dist/` directory below is expected to sit at the
# build context root, exactly what the release workflow's `docker` job downloads before building).
FROM python:3.12-slim

# Pinned to the same uv release the release workflow installs (see .github/workflows/release.yml).
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /usr/local/bin/

# git, gh and jq are used throughout the bundled workflows (conflict-list construction, gh-based
# PR steps). The GitHub CLI has no Debian-archive package, so its own apt repository is added.
RUN apt-get update && apt-get install -y --no-install-recommends git jq ca-certificates curl gnupg \
    && mkdir -p -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# The sdist + wheel `uv build` produced (release.yml's `build` job, artifact `dist`). `openai-codex`
# and `claude-agent-sdk` are rayspec's own dependencies, named again here for clarity: both ship
# their agent CLI binary inside the wheel, so installing the package IS the pre-warm step — there
# is nothing left to fetch at container run time.
COPY dist/ /tmp/dist/
RUN uv pip install --system --no-cache-dir /tmp/dist/*.whl claude-agent-sdk openai-codex \
    && rm -rf /tmp/dist

# Fixed UID 1000: a bind-mounted host directory (e.g. -v $HOME/.rayspec:/home/rayspec/.rayspec)
# keeps matching ownership across `docker run` invocations. Documented again in README.md.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin rayspec
USER rayspec
WORKDIR /home/rayspec

ENTRYPOINT ["rayspec"]
