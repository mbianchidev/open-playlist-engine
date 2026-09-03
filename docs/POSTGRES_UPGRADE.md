# Upgrade PostgreSQL 17 to 18

Open Playlist Engine uses PostgreSQL 18 and mounts its data volume at
`/var/lib/postgresql`. PostgreSQL 18 introduced this parent-directory layout so
major-version data directories can coexist during upgrades.

An existing PostgreSQL 17 volume cannot be started directly by PostgreSQL 18.
The repository includes an offline dump-and-restore migration that creates a
separate PostgreSQL 18 volume and retains the PostgreSQL 17 volume for rollback.

The migration targets the stock Compose database: database `ope`, role `ope`, and
the default Compose credentials. It stops if the cluster contains additional
user databases or roles; migrate customized PostgreSQL clusters with standard
PostgreSQL tooling instead.

## Before upgrading

1. Pull the release containing the PostgreSQL 18 Compose configuration.
2. Ensure `.env` exists and still contains the settings used by the deployment.
3. Ensure Docker has enough free space for the old volume, a temporary logical
   backup, and the new volume.
4. Do not run `docker compose down --volumes`; that deletes managed data.

The default source volume is `open-playlist-engine_pgdata`. If the deployment
uses a different Compose project name, export the same `COMPOSE_PROJECT_NAME`
used to start it.

## Run the migration

```bash
./scripts/migrate-postgres-17-to-18.sh
docker compose up --detach
```

The migration script:

1. stops the Compose application without deleting volumes;
2. starts the retained volume with PostgreSQL 17;
3. creates a temporary custom-format logical backup;
4. records table row counts and sequence values;
5. creates `open-playlist-engine_pgdata18` using the PostgreSQL 18 volume layout;
6. restores the backup in a single transaction;
7. compares the PostgreSQL 17 and 18 verification manifests; and
8. starts the migrated PostgreSQL 18 service through Compose.

The temporary backup is deleted when the script exits. The PostgreSQL 17 source
volume is not deleted.

For a custom project name:

```bash
export COMPOSE_PROJECT_NAME=my-instance
./scripts/migrate-postgres-17-to-18.sh
docker compose up --detach
```

## Failure and retry

If migration fails, the script removes its temporary containers and reports the
incomplete PostgreSQL 18 volume. The PostgreSQL 17 source volume remains
available.

Remove only the reported PostgreSQL 18 target before retrying:

```bash
docker volume rm "${COMPOSE_PROJECT_NAME:-open-playlist-engine}_pgdata18"
./scripts/migrate-postgres-17-to-18.sh
```

Do not remove the PostgreSQL 17 source volume.

## Rollback

Stop the PostgreSQL 18 deployment and redeploy the previous application release,
whose Compose file uses PostgreSQL 17 and `open-playlist-engine_pgdata`.

```bash
docker compose down
```

The old volume remains a complete rollback point until it is explicitly deleted.
Writes accepted by PostgreSQL 18 after cutover are not copied back to PostgreSQL
17, so rollback loses those newer writes.

After the PostgreSQL 18 deployment has been accepted and backed up, remove the
old volume only when rollback is no longer required:

```bash
docker volume rm "${COMPOSE_PROJECT_NAME:-open-playlist-engine}_pgdata"
```
