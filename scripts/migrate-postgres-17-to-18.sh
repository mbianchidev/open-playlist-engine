#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
cd "$repo_root"

project_name="${COMPOSE_PROJECT_NAME:-open-playlist-engine}"
source_volume="${project_name}_pgdata"
target_volume="${project_name}_pgdata18"
source_container="${project_name}-postgres17-migration-$$"
target_container="${project_name}-postgres18-migration-$$"
wait_seconds="${POSTGRES_MIGRATION_WAIT_SECONDS:-60}"
temporary_root="${POSTGRES_MIGRATION_TMPDIR:-${TMPDIR:-/tmp}}"
work_dir=""
target_created=0
migration_complete=0

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  local status=$?
  trap - EXIT
  set +e
  docker rm --force "$source_container" "$target_container" >/dev/null 2>&1
  if [ "$target_created" -eq 1 ] && [ "$migration_complete" -eq 0 ]; then
    docker compose rm --force --stop postgres >/dev/null 2>&1
  fi
  if [ -n "$work_dir" ]; then
    rm -rf "$work_dir"
  fi
  if [ "$status" -ne 0 ]; then
    printf 'The PostgreSQL 17 source volume remains available at %s.\n' "$source_volume" >&2
    if [ "$target_created" -eq 1 ] && [ "$migration_complete" -eq 0 ]; then
      printf 'Remove the incomplete target before retrying: docker volume rm %s\n' "$target_volume" >&2
    fi
  fi
  exit "$status"
}

wait_for_postgres() {
  local container="$1"
  local attempt
  local state

  for ((attempt = 1; attempt <= wait_seconds; attempt++)); do
    if docker exec "$container" psql \
      --no-psqlrc \
      --username ope \
      --dbname ope \
      --command "SELECT 1;" >/dev/null 2>&1; then
      return
    fi

    state="$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || true)"
    if [ "$state" != "running" ]; then
      docker logs "$container" >&2 || true
      fail "$container stopped before PostgreSQL became ready"
    fi
    sleep 1
  done

  docker logs "$container" >&2 || true
  fail "PostgreSQL did not become ready within ${wait_seconds} seconds"
}

capture_database_manifest() {
  local container="$1"
  local output="$2"
  local relation
  local row_count

  : >"$output"

  docker exec "$container" psql \
    --no-psqlrc \
    --tuples-only \
    --no-align \
    --username ope \
    --dbname ope \
    --command "
      SELECT
        'sequence' || E'\t' ||
        format('%I.%I', schemaname, sequencename) || E'\t' ||
        COALESCE(last_value::text, 'NULL')
      FROM pg_sequences
      WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
      ORDER BY 1;
    " >>"$output"

  while IFS= read -r relation; do
    [ -n "$relation" ] || continue
    row_count="$(
      docker exec "$container" psql \
        --no-psqlrc \
        --tuples-only \
        --no-align \
        --username ope \
        --dbname ope \
        --command "SELECT count(*) FROM ${relation};"
    )"
    printf 'table\t%s\t%s\n' "$relation" "$row_count" >>"$output"
  done < <(
    docker exec "$container" psql \
      --no-psqlrc \
      --tuples-only \
      --no-align \
      --username ope \
      --dbname ope \
      --command "
        SELECT format('%I.%I', schemaname, tablename)
        FROM pg_tables
        WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY 1;
      "
  )

  sort -o "$output" "$output"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

command -v docker >/dev/null 2>&1 || fail "docker is required"
[[ "$wait_seconds" =~ ^[1-9][0-9]*$ ]] || fail "POSTGRES_MIGRATION_WAIT_SECONDS must be a positive integer"
[ -f .env ] || fail "create .env from .env.example before running the migration"
docker info >/dev/null 2>&1 || fail "Docker is not available"
docker compose config --quiet
docker volume inspect "$source_volume" >/dev/null 2>&1 ||
  fail "source volume $source_volume does not exist"
if docker volume inspect "$target_volume" >/dev/null 2>&1; then
  fail "target volume $target_volume already exists"
fi

work_dir="$(mktemp -d "${temporary_root%/}/ope-postgres-upgrade.XXXXXX")"
dump_file="$work_dir/ope.dump"
source_manifest="$work_dir/source-manifest.txt"
target_manifest="$work_dir/target-manifest.txt"

printf 'Stopping the application without deleting volumes...\n'
docker compose down --remove-orphans

printf 'Starting PostgreSQL 17 from %s...\n' "$source_volume"
docker run \
  --detach \
  --name "$source_container" \
  --env POSTGRES_PASSWORD=ope \
  --volume "${source_volume}:/var/lib/postgresql/data" \
  postgres:17-alpine >/dev/null
wait_for_postgres "$source_container"

source_version="$(
  docker exec "$source_container" psql \
    --no-psqlrc \
    --tuples-only \
    --no-align \
    --username ope \
    --dbname ope \
    --command "SHOW server_version_num;"
)"
if [ "$source_version" -lt 170000 ] || [ "$source_version" -ge 180000 ]; then
  fail "expected PostgreSQL 17 in $source_volume, found server_version_num=$source_version"
fi

extra_databases="$(
  docker exec "$source_container" psql \
    --no-psqlrc \
    --tuples-only \
    --no-align \
    --username ope \
    --dbname postgres \
    --command "
      SELECT COALESCE(string_agg(datname, ', ' ORDER BY datname), '')
      FROM pg_database
      WHERE datallowconn
        AND NOT datistemplate
        AND datname NOT IN ('ope', 'postgres');
    "
)"
[ -z "$extra_databases" ] ||
  fail "unsupported additional databases found: $extra_databases"

extra_roles="$(
  docker exec "$source_container" psql \
    --no-psqlrc \
    --tuples-only \
    --no-align \
    --username ope \
    --dbname postgres \
    --command "
      SELECT COALESCE(string_agg(rolname, ', ' ORDER BY rolname), '')
      FROM pg_roles
      WHERE rolname !~ '^pg_'
        AND rolname <> 'ope';
    "
)"
[ -z "$extra_roles" ] ||
  fail "unsupported additional roles found: $extra_roles"

printf 'Creating a logical backup and verification manifest...\n'
capture_database_manifest "$source_container" "$source_manifest"
docker exec "$source_container" pg_dump \
  --username ope \
  --dbname ope \
  --format custom \
  --no-owner \
  --no-acl >"$dump_file"
[ -s "$dump_file" ] || fail "pg_dump produced an empty backup"

docker rm --force "$source_container" >/dev/null

printf 'Creating PostgreSQL 18 volume %s...\n' "$target_volume"
docker compose create postgres >/dev/null
target_created=1
compose_postgres_container="$(docker compose ps --all --quiet postgres)"
[ -n "$compose_postgres_container" ] ||
  fail "Docker Compose did not create the PostgreSQL 18 container"
compose_target_volume="$(
  docker inspect \
    --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql"}}{{.Name}}{{end}}{{end}}' \
    "$compose_postgres_container"
)"
[ "$compose_target_volume" = "$target_volume" ] ||
  fail "Docker Compose created unexpected target volume $compose_target_volume"
docker compose rm --force --stop postgres >/dev/null

docker run \
  --detach \
  --name "$target_container" \
  --env POSTGRES_USER=ope \
  --env POSTGRES_PASSWORD=ope \
  --env POSTGRES_DB=ope \
  --volume "${target_volume}:/var/lib/postgresql" \
  postgres:18-alpine >/dev/null
wait_for_postgres "$target_container"

target_version="$(
  docker exec "$target_container" psql \
    --no-psqlrc \
    --tuples-only \
    --no-align \
    --username ope \
    --dbname ope \
    --command "SHOW server_version_num;"
)"
if [ "$target_version" -lt 180000 ] || [ "$target_version" -ge 190000 ]; then
  fail "expected PostgreSQL 18, found server_version_num=$target_version"
fi

printf 'Restoring the logical backup into PostgreSQL 18...\n'
docker exec --interactive "$target_container" pg_restore \
  --username ope \
  --dbname ope \
  --no-owner \
  --no-acl \
  --exit-on-error \
  --single-transaction <"$dump_file"
docker exec "$target_container" psql \
  --no-psqlrc \
  --username ope \
  --dbname ope \
  --command "ANALYZE;" >/dev/null

printf 'Comparing table row counts and sequence values...\n'
capture_database_manifest "$target_container" "$target_manifest"
if ! diff --unified "$source_manifest" "$target_manifest"; then
  fail "verification manifest differs after restore"
fi

data_directory="$(
  docker exec "$target_container" psql \
    --no-psqlrc \
    --tuples-only \
    --no-align \
    --username ope \
    --dbname ope \
    --command "SHOW data_directory;"
)"
case "$data_directory" in
  /var/lib/postgresql/18/*) ;;
  *) fail "PostgreSQL 18 used unexpected data directory $data_directory" ;;
esac

docker rm --force "$target_container" >/dev/null

printf 'Starting PostgreSQL 18 through Docker Compose...\n'
docker compose up --detach --wait postgres
migration_complete=1

printf '\nPostgreSQL migration completed.\n'
printf 'Rollback source retained: %s\n' "$source_volume"
printf 'Active PostgreSQL 18 data: %s\n' "$target_volume"
printf 'Start the remaining services with: docker compose up --detach\n'
