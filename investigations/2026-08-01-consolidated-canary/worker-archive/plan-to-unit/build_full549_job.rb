#!/usr/bin/env ruby

require "date"
require "yaml"

source = YAML.safe_load(
  STDIN.read,
  permitted_classes: [Time, Date],
  aliases: true,
)

source.fetch("metadata")["name"] = "jugnu-plan60-full549-v2"

job_spec = source.fetch("spec").fetch("template").fetch("spec")
job_spec["taskCount"] = 100
job_spec["parallelism"] = 50

container = job_spec.fetch("template").fetch("spec").fetch("containers").first
container["image"] = ARGV.fetch(1)

desired = {
  "CSV_GCS_URI" => "gs://jugnu-canary/property-list/plan60-codex-549-v2.csv",
  "RUN_DATE" => "2026-08-01-plan60-full549-v2",
  "PROFILE_GCS_PREFIX" => "gs://jugnu-canary/profiles/plan60-full549-v2",
  "DATA_PROVIDER" => "filesystem",
  "BUCKET" => "jugnu-canary",
  "BUCKET_NAME" => "jugnu-canary",
  "ENABLE_TIER4_LLM" => "false",
  "COMPLIANCE_MODE" => "1",
  "ENABLE_UNLOCKER_TIER" => "false",
  "ENABLE_FLARESOLVERR_TIER" => "false",
  "ENABLE_RESIDENTIAL_TIER" => "false",
  "ENABLE_RESIDENTIAL_RENDER_TIER" => "false",
  "ENABLE_DC_PROXY_TIER" => "false",
  "WEB_UNLOCKER_MAX_CALLS_PER_JOB" => "0",
  "WEB_UNLOCKER_MAX_CALLS_PER_PROPERTY" => "0",
  "FETCH_BACKEND" => "hyperbrowser",
  "HYPERBROWSER_MAX_CALLS_PER_PROPERTY" => "3",
  "HB_USE_STEALTH" => "false",
  "HB_USE_PROXY" => "true",
  "INTERACTION_REVEAL" => "false",
}

blocked = ["WEB_UNLOCKER_KEY", "WEB_UNLOCKER_ZONE"]
environment = container.fetch("env", []).reject { |entry| blocked.include?(entry["name"]) }
indexed = environment.to_h { |entry| [entry["name"], entry] }

desired.each do |name, value|
  entry = indexed[name]
  if entry
    entry.delete("valueFrom")
    entry["value"] = value
  else
    entry = {"name" => name, "value" => value}
    environment << entry
    indexed[name] = entry
  end
end

hyperbrowser = indexed["HYPERBROWSER_API_KEY"]
unless hyperbrowser
  hyperbrowser = {"name" => "HYPERBROWSER_API_KEY"}
  environment << hyperbrowser
end
hyperbrowser.delete("value")
hyperbrowser["valueFrom"] = {
  "secretKeyRef" => {
    "key" => "latest",
    "name" => "hyperbrowser-api-key",
  },
}

container["env"] = environment

output_path = ARGV.fetch(0)
File.open(output_path, File::WRONLY | File::CREAT | File::TRUNC, 0o600) do |file|
  file.write(YAML.dump(source))
end
