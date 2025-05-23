import requests
import json
import collections
from datetime import datetime, timedelta

import pandas as pd
from packaging.version import Version


PY_RELEASES = {
    "3.8": "Oct 14, 2019",
    "3.9": "Oct 5, 2020",
    "3.10": "Oct 4, 2021",
    "3.11": "Oct 24, 2022",
    "3.12": "Oct 2, 2023",
    "3.13": "Oct 7, 2024",
}
CORE_PACKAGES = [
    "ipython",
    "matplotlib",
    "networkx",
    "numpy",
    "pandas",
    "scikit-image",
    "scikit-learn",
    "scipy",
    "xarray",
    "zarr",
]
PLUS_36_MONTHS = timedelta(days=int(365 * 3))
PLUS_24_MONTHS = timedelta(days=int(365 * 2))

# Release data
# We put the cutoff at 3 quarters ago - we do not use "just" -9 months
# to avoid the content of the quarter to change depending on when we
# generate this file during the current quarter.
CURRENT_DATE = pd.Timestamp.now()
CURRENT_QUARTER_START = pd.Timestamp(
    CURRENT_DATE.year, (CURRENT_DATE.quarter - 1) * 3 + 1, 1
)
CUTOFF = CURRENT_QUARTER_START - pd.DateOffset(months=9)


def get_release_dates(package, support_time=PLUS_24_MONTHS):
    releases = {}
    print(f"Querying pypi.org for {package} versions...", end="", flush=True)
    response = requests.get(
        f"https://pypi.org/simple/{package}",
        headers={"Accept": "application/vnd.pypi.simple.v1+json"},
    ).json()
    print("OK")
    file_date = collections.defaultdict(list)
    for f in response["files"]:
        if f["filename"].endswith(".tar.gz") or f["filename"].endswith(".zip"):
            continue
        ver = f["filename"].split("-")[1]
        try:
            version = Version(ver)
        except Exception:
            continue
        if version.is_prerelease or version.micro != 0:
            continue
        release_date = pd.Timestamp(f["upload-time"]).tz_localize(None)
        if not release_date:
            continue
        file_date[version].append(release_date)
    release_date = {v: min(file_date[v]) for v in file_date}
    for ver, release_date in sorted(release_date.items()):
        drop_date = release_date + support_time
        if drop_date >= CUTOFF:
            releases[ver] = {
                "release_date": release_date,
                "drop_date": drop_date,
            }
    return releases


package_releases = {
    "python": {
        version: {
            "release_date": datetime.strptime(release_date, "%b %d, %Y"),
            "drop_date": datetime.strptime(release_date, "%b %d, %Y") + PLUS_36_MONTHS,
        }
        for version, release_date in PY_RELEASES.items()
    }
}
package_releases |= {package: get_release_dates(package) for package in CORE_PACKAGES}
# Filter all items whose drop_date are in the past
package_releases = {
    package: {
        version: dates
        for version, dates in releases.items()
        if dates["drop_date"] > CUTOFF
    }
    for package, releases in package_releases.items()
}

# Save Gantt chart
print("Saving Mermaid chart to chart.md")
with open("chart.md", "w") as fh:
    fh.write(
        """gantt
dateFormat YYYY-MM-DD
axisFormat %m / %Y
title Support Window"""
    )
    for name, releases in package_releases.items():
        fh.write(f"\n\nsection {name}")
        for version, dates in releases.items():
            fh.write(
                f"\n{version} : {dates['release_date'].strftime('%Y-%m-%d')},{dates['drop_date'].strftime('%Y-%m-%d')}"
            )
    fh.write("\n")

# Print drop schedule
data = []
for k, versions in package_releases.items():
    for v, dates in versions.items():
        data.append(
            (
                k,
                v,
                pd.to_datetime(dates["release_date"]),
                pd.to_datetime(dates["drop_date"]),
            )
        )
df = pd.DataFrame(data, columns=["package", "version", "release", "drop"])
df["quarter"] = df["drop"].dt.to_period("Q")
df["new_min_version"] = (
    df[["package", "version", "quarter"]].groupby("package").shift(-1)["version"]
)
dq = df.set_index(["quarter", "package"]).sort_index().dropna()
new_min_versions = (
    dq.groupby(["quarter", "package"]).agg({"new_min_version": "max"}).reset_index()
)

# We want to build a dict with the structure [{start_date: timestamp, packages: {package: lower_bound}}]
new_min_versions_list = []
for q, packages in new_min_versions.groupby("quarter"):
    package_lower_bounds = {
        p: str(v) for p, v in packages.drop("quarter", axis=1).itertuples(index=False)
    }
    # jq is really insistent the Z should be there
    quarter_start_time_str = str(q.start_time.isoformat()) + "Z"
    new_min_versions_list.append(
        {"start_date": quarter_start_time_str, "packages": package_lower_bounds}
    )
print("Saving drop schedule to schedule.json")
with open("schedule.json", "w") as f:
    f.write(json.dumps(new_min_versions_list, sort_keys=True))


def pad_table(table):
    rows = [[el.strip() for el in row.split("|")] for row in table]
    col_widths = [max(map(len, column)) for column in zip(*rows)]
    rows[1] = [
        el if el != "----" else "-" * col_widths[i] for i, el in enumerate(rows[1])
    ]
    padded_table = []
    for row in rows:
        line = ""
        for entry, width in zip(row, col_widths):
            if not width:
                continue
            line += f"| {str.ljust(entry, width)} "
        line += "|"
        padded_table.append(line)
    return padded_table


def make_table(sub):
    table = []
    table.append("|    |    |    |")
    table.append("|----|----|----|")
    for package in sorted(set(sub.index.get_level_values(0))):
        vers = sub.loc[[package]]["version"]
        minv, maxv = min(vers), max(vers)
        rels = sub.loc[[package]]["release"]
        rel_min, rel_max = min(rels), max(rels)
        version_range = str(minv) if minv == maxv else f"{minv} to {maxv}"
        rel_range = (
            str(rel_min.strftime("%b %Y"))
            if rel_min == rel_max
            else f"{rel_min.strftime('%b %Y')} and {rel_max.strftime('%b %Y')}"
        )
        table.append(f"|{package:<15}|{version_range:<19}|released {rel_range}|")
    return pad_table(table)


def make_quarter(quarter, dq):
    table = ["#### " + str(quarter).replace("Q", " - Quarter ") + ":\n"]
    table.append("###### Recommend drop support for:\n")
    sub = dq.loc[quarter]
    table.extend(make_table(sub))
    return "\n".join(table)


print("Saving drop schedule to schedule.md")
with open("schedule.md", "w") as fh:
    # We collect packages 6 month in the past, and drop the first quarter
    # as we might have filtered some of the packages out depending on
    # when we ran the script.
    tb = []
    for quarter in list(sorted(set(dq.index.get_level_values(0))))[1:]:
        tb.append(make_quarter(quarter, dq))
    fh.write("\n\n".join(tb))
    fh.write("\n")
