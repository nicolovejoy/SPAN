# Template for /etc/span-backup.env on phrpi.
#
# Contains only 1Password secret references — no secrets — so it is safe to
# commit. Fill it with:
#
#     op inject -i span-backup.env.tpl -o /tmp/span-backup.env
#
# NOTE: op inject parses this entire file, comments included — both bare
# references and double-brace templates. Never write either form in prose
# here, only on the real assignment lines below. Explaining the syntax inside
# a comment is enough to break the parse.
#
# The Pi has no 1Password, so the *rendered* file lives there as plaintext,
# root-owned and chmod 600. That is intentional: the Pi is the one place in
# this setup that can't resolve references at runtime.
#
# If a reference below fails, the field label in the item doesn't match. Open
# the item in the 1Password desktop app and use the exact labels shown there.

# Full repo URL including your Cloudflare account ID. Stored in the
# `repository` field of the phrpi-restic-backup item — make sure you replaced
# the ACCOUNT_ID placeholder there with your real account ID.
RESTIC_REPOSITORY={{ op://dev-secrets/phrpi-restic-backup/repository }}

# R2 API token, scoped to Object Read & Write on the phrpi-backups bucket.
# restic talks to R2 over its S3-compatible endpoint, so it needs the S3
# credential pair — not the "Token Value" field, which is for Cloudflare's
# own API and is unused here.
AWS_ACCESS_KEY_ID={{ op://dev-secrets/phrpi-backups-api-token/Access Key ID }}
AWS_SECRET_ACCESS_KEY={{ op://dev-secrets/phrpi-backups-api-token/Secret Access Key }}

# restic's client-side encryption password. Losing this loses every backup.
RESTIC_PASSWORD={{ op://dev-secrets/phrpi-restic-backup/password }}

# R2 ignores region, but the S3 client requires a value.
AWS_DEFAULT_REGION=auto
