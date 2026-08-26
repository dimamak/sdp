# Auto-deploy on push (optional)

`.github/workflows/deploy.yml` deploys `main` to your server automatically on
every push, over SSH into a forced command. Nothing about it is required —
`server/deploy.sh` still works for manual deploys if you'd rather not set
this up.

How it works: GitHub Actions checks out the repo, tars it up (same excludes
as `server/deploy.sh`: `.venv`, `data`, `logs`, `config.yaml`, `.env`,
instance secrets, etc. — none of these are tracked in git anyway), and pipes
the tarball over SSH to your server using a key that's restricted, via
`command=` in `authorized_keys`, to run nothing but `server/ci_deploy.sh`.
That script extracts the tree, waits out any in-progress nightly run first,
updates `.last-push-commit`, and restarts the `dailypost-bot*` services.

## 1. Generate a deploy-only key

On your workstation (not the server, so the private key never touches the
box it can log into):

```bash
ssh-keygen -t ed25519 -f ./dailypost-deploy-key -N "" -C "dailypost-ci-deploy"
```

## 2. Install the public key on the server, restricted

On the server, as whatever user owns `/opt/dailypost/app` (adjust the path
if yours differs):

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo 'restrict,command="/opt/dailypost/app/server/ci_deploy.sh /opt/dailypost/app" ssh-ed25519 AAAA...your-pubkey... dailypost-ci-deploy' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

The `command=` argument is the only place the app directory is configured —
`ci_deploy.sh` itself takes it as `$1` and hardcodes nothing, so this is the
one line you'd change for a different layout. `restrict` (OpenSSH 7.2+)
disables port/X11/agent forwarding and PTY allocation, so even if the
private key leaked, it can only ever run that one script.

`server/ci_deploy.sh` needs to already exist at that path before the first
deploy runs (the forced command references a file, not incoming content) —
either deploy once manually with `server/deploy.sh` first, or `scp` just
that one file over.

## 3. Pin your server's host key

From your workstation:

```bash
ssh-keyscan -t ed25519 your.server.host
```

Copy the single line of output — you'll paste it into a secret next.

## 4. Add repo secrets

In GitHub: repo → Settings → Secrets and variables → Actions → New repository secret.

| Secret | Value |
|---|---|
| `DEPLOY_SSH_KEY` | contents of the private half from step 1 |
| `DEPLOY_SSH_HOST` | your server's hostname or IP |
| `DEPLOY_SSH_USER` | the user from step 2 |
| `DEPLOY_SSH_KNOWN_HOSTS` | the line from step 3 |

None of these live in the repo — that's the point of using secrets instead
of hardcoding a personal server's address in a public workflow file.

## 5. Point the workflow at your fork

`.github/workflows/deploy.yml` only runs on `dimamak/sdp` (so forks
don't get a failing Action from missing secrets on every push). If you're
running your own fork, change that `if:` condition to your `owner/repo`.

## Notes

- Deploys are additive-only (`cp -a`, no delete) — removing a file from the
  repo doesn't remove it from the server. Same behavior as `deploy.sh` today.
- If a deploy lands while a nightly run is mid-flight, `ci_deploy.sh` waits
  up to 5 minutes for the lock to clear, then aborts (non-zero exit, visible
  as a failed Action) rather than risk swapping code under a running job.
