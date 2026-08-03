#!/usr/bin/env ruby

# Build the immutable, isolated Cloud Run job for the post-fix verification
# cohort. The source job is supplied on stdin so secret references remain
# references; this script only changes release coordinates and non-secret
# feature flags.

require "date"
require "yaml"

output_path = ARGV.fetch(0)
image_digest = ARGV.fetch(1)
commit = ARGV.fetch(2)
sample_sha = ARGV.fetch(3)
property_count = Integer(ARGV.fetch(4))
property_timeout_seconds = Integer(ARGV.fetch(5, "600"))
task_timeout_seconds = Integer(ARGV.fetch(6, "14400"))
runner_timeout_seconds = Integer(ARGV.fetch(7, "13500"))
short_commit = commit[0, 7]

source = YAML.safe_load(
  STDIN.read,
  permitted_classes: [Time, Date],
  aliases: true,
)

metadata = source.fetch("metadata")
metadata["name"] = "jugnu-verify-#{short_commit}"
metadata.delete("namespace")
metadata.delete("annotations")
metadata["labels"] = {
  "canary" => "fix-verification",
  "commit" => short_commit,
  "profile-set" => "isolated",
  "sample-sha" => sample_sha[0, 7],
}

job_spec = source.fetch("spec").fetch("template").fetch("spec")
job_spec["taskCount"] = property_count
job_spec["parallelism"] = [property_count, 30].min
job_spec.fetch("template").delete("metadata")

task_spec = job_spec.fetch("template").fetch("spec")
task_spec["timeoutSeconds"] = task_timeout_seconds.to_s
task_spec["maxRetries"] = 0
container = task_spec.fetch("containers").first
container["image"] = image_digest

run_slug = "verify-#{short_commit}"
desired = {
  "BROWSERS_PER_TASK" => "3",
  "CSV_GCS_URI" => "gs://jugnu-canary/property-list/#{run_slug}.csv",
  "RUN_DATE" => "2026-08-02-#{run_slug}",
  "PROFILE_GCS_PREFIX" => "gs://jugnu-canary/profiles/#{run_slug}/",
  "DATA_PROVIDER" => "filesystem",
  "BUCKET" => "jugnu-canary",
  "BUCKET_NAME" => "jugnu-canary",
  "SHARD_SOURCE" => "csv",
  "ENABLE_TIER4_LLM" => "false",
  "COMPLIANCE_MODE" => "1",
  "ENABLE_UNLOCKER_TIER" => "false",
  "ENABLE_FLARESOLVERR_TIER" => "false",
  "ENABLE_DC_PROXY_TIER" => "false",
  "WEB_UNLOCKER_MAX_CALLS_PER_JOB" => "0",
  "WEB_UNLOCKER_MAX_CALLS_PER_PROPERTY" => "0",
  "FETCH_BACKEND" => "hyperbrowser",
  "HYPERBROWSER_MAX_CALLS_PER_PROPERTY" => "3",
  "HYPERBROWSER_RESERVED_PRIORITY_CALLS" => "1",
  "HB_USE_STEALTH" => "false",
  "HB_USE_PROXY" => "true",
  "PER_PROPERTY_TIMEOUT_SECONDS" => property_timeout_seconds.to_s,
  "SHARD_RUNNER_TIMEOUT_SECONDS" => runner_timeout_seconds.to_s,
}

blocked = ["WEB_UNLOCKER_KEY", "WEB_UNLOCKER_ZONE"]
environment = container.fetch("env", []).reject { |entry| blocked.include?(entry["name"]) }
indexed = environment.to_h { |entry| [entry["name"], entry] }
desired.each do |name, value|
  entry = indexed[name]
  unless entry
    entry = {"name" => name}
    environment << entry
    indexed[name] = entry
  end
  entry.delete("valueFrom")
  entry["value"] = value
end
container["env"] = environment

File.open(output_path, File::WRONLY | File::CREAT | File::TRUNC, 0o600) do |file|
  file.write(YAML.dump(source))
end
