#!/usr/bin/python3
from __future__ import annotations

import json
import subprocess
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from pathlib import Path


OUTPUT_FILE = Path(__file__).with_name("monitor.json")
MAX_FILE_SIZE = 50 * 1024 * 1024
INTERFACES = ["wlan0", "wlan1", "hotspot", "eth0", "usb0", "wwan0"]


def ist_now() -> datetime:
	return datetime.now(timezone(timedelta(hours=5, minutes=30)))


def timestamp_string() -> str:
	return ist_now().strftime("%a %d %b %Y %I:%M:%S %p IST")


def run_command(command: list[str]) -> str:
	try:
		completed = subprocess.run(
			command,
			capture_output=True,
			text=True,
			timeout=30,
			check=False,
		)
	except Exception as exc:
		return f"ERROR: {exc}"

	output = completed.stdout.strip()
	error = completed.stderr.strip()

	if completed.returncode != 0:
		if output and error:
			return f"{output}\n{error}"
		return output or error or f"ERROR: command failed with exit code {completed.returncode}"

	return output


def run_shell_command(command: str) -> str:
	try:
		completed = subprocess.run(
			command,
			shell=True,
			capture_output=True,
			text=True,
			timeout=30,
			check=False,
		)
	except Exception as exc:
		return f"ERROR: {exc}"

	output = completed.stdout.strip()
	error = completed.stderr.strip()

	if completed.returncode != 0:
		if output and error:
			return f"{output}\n{error}"
		return output or error or f"ERROR: command failed with exit code {completed.returncode}"

	return output


def collect_container_ip(container_id: str) -> str:
	return run_command([
		"docker",
		"inspect",
		"-f",
		"{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
		container_id,
	])


def collect_docker_ps() -> list[dict[str, str]]:
	raw_output = run_command([
		"docker",
		"ps",
		"--format",
		"{{.ID}}\t{{.Image}}\t{{.CreatedAt}}\t{{.Status}}",
	])

	if raw_output.startswith("ERROR:"):
		return [{"CONTAINER": "", "IMAGE": raw_output, "CREATED": "", "STATUS": "", "IPADDRESS": ""}]

	containers: list[dict[str, str]] = []
	for line in raw_output.splitlines():
		parts = line.split("\t")
		if len(parts) != 4:
			continue
		container_id, image, created, status = parts
		containers.append(
			{
				"CONTAINER": container_id,
				"IMAGE": image,
				"CREATED": created,
				"STATUS": status,
				"IPADDRESS": collect_container_ip(container_id),
			}
		)

	return containers


def collect_interface_data(interface_name: str) -> dict[str, str]:
	return {
		"ifconfig": run_command(["ifconfig", interface_name]),
		"ping": run_command(["ping", "-I", interface_name, "-c", "1", "8.8.8.8"]),
	}


def collect_snapshot() -> dict[str, object]:
	return {
		"timestamp": timestamp_string(),
		"uptime": run_command(["uptime"]),
		"uptime_s": run_command(["uptime", "-s"]),
		"lsusb": run_command(["lsusb"]),
		"docker_ps": collect_docker_ps(),
		"last_hardware_sending_data": run_shell_command("docker logs hardware -t | grep '\\[MANAGER\\] Sending Data' | tail -n2"),
		"interfaces": OrderedDict(
			(interface_name, collect_interface_data(interface_name))
			for interface_name in INTERFACES
		),
	}


def load_records() -> list[dict[str, object]]:
	if not OUTPUT_FILE.exists():
		return []

	try:
		with OUTPUT_FILE.open("r", encoding="utf-8") as handle:
			data = json.load(handle)
	except (OSError, json.JSONDecodeError):
		return []

	if isinstance(data, list):
		return data
	return []


def dump_records(records: list[dict[str, object]]) -> None:
	tmp_file = OUTPUT_FILE.with_suffix(".json.tmp")
	with tmp_file.open("w", encoding="utf-8") as handle:
		json.dump(records, handle, ensure_ascii=True, separators=(",", ":"))
	tmp_file.replace(OUTPUT_FILE)


def save_snapshot(snapshot: dict[str, object]) -> None:
	records = load_records()
	records.append(snapshot)

	while records:
		dump_records(records)
		if OUTPUT_FILE.stat().st_size <= MAX_FILE_SIZE:
			return
		records.pop(0)

	dump_records([])


def main() -> None:
	save_snapshot(collect_snapshot())


if __name__ == "__main__":
	main()
