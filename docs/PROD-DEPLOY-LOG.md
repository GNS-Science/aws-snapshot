# Production deploy log — moved

The production deploy log has moved to the private operational shim
repo `gns-science/nzshm-backup-ops` (see `docs/PROD-DEPLOY-LOG.md`
there). The previous public copy contained GNS-specific account IDs,
operator names, and incident timestamps that don't belong in the
OSS-facing engine repo.

If you're a GNS operator looking for the log, it lives in your local
clone of `nzshm-backup-ops`. If you're an OSS user wondering where
deploy history lives for *your* install, see the migration plan
section §3a (shim-repo strategy) in `docs/design/open-source-migration.md`.
