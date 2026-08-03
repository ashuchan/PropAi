#!/usr/bin/env ruby

# Build the immutable stratified-1000 Cloud Run Job definition by cloning the
# last successful affected-386 job.  Secret references are preserved; only
# non-secret launch coordinates and release labels are changed.

require "date"
require "yaml"

output_path = ARGV.fetch(0)
image_digest = ARGV.fetch(1)
source = YAML.safe_load(
  STDIN.read,
  permitted_classes: [Time, Date],
  aliases: true,
)

metadata = source.fetch("metadata")
metadata["name"] = "jugnu-strat1000-ff7b377"
metadata.delete("namespace")
metadata.delete("annotations")
metadata["labels"] = {
  "canary" => "stratified-1000",
  "commit" => "ff7b377",
  "profile-set" => "identity-v2",
  "sample-sha" => "0a51ad2",
}

job_spec = source.fetch("spec").fetch("template").fetch("spec")
job_spec["taskCount"] = 100
job_spec["parallelism"] = 50
job_spec.fetch("template").delete("metadata")

task_spec = job_spec.fetch("template").fetch("spec")
task_spec["timeoutSeconds"] = "14400"
task_spec["maxRetries"] = 0
container = task_spec.fetch("containers").first
container["image"] = image_digest

desired = {
  "BROWSERS_PER_TASK" => "3",
  "CSV_GCS_URI" => "gs://jugnu-canary/property-list/stratified1000-ff7b377.csv",
  "RUN_DATE" => "2026-08-02-strat1000-ff7b377",
  "PROFILE_GCS_PREFIX" => "gs://jugnu-canary/profiles/strat1000-ff7b377/",
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
