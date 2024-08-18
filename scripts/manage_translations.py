#!/usr/bin/env python
#
# This Python file contains utility scripts to manage Django translations.
# It has to be run inside the django git root directory.
#
# The following commands are available:
#
# * update_catalogs: check for new strings in core and contrib catalogs, and
#                    output how much strings are new/changed.
#
# * lang_stats: output statistics for each catalog/language combination
#
# * fetch: fetch translations from transifex.com
#
# Each command support the --languages and --resources options to limit their
# operation to the specified language or resource. For example, to get stats
# for Spanish in contrib.admin, run:
#
#  $ python scripts/manage_translations.py lang_stats --language=es --resources=admin


import os
from argparse import ArgumentParser
from collections import defaultdict
from configparser import ConfigParser
from datetime import datetime
from subprocess import run, CalledProcessError, PIPE
import logging
import sys

import requests

import django
from django.conf import settings
from django.core.management import call_command

HAVE_JS = ["admin"]
LANG_OVERRIDES = {
    "zh_CN": "zh_Hans",
    "zh_TW": "zh_Hant",
}

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

def list_resources_with_updates(date_since, date_skip=None, verbose=False):
    resource_lang_changed = defaultdict(list)
    resource_lang_unchanged = defaultdict(list)

    # Read token from ENV, otherwise read from the ~/.transifexrc file.
    api_token = os.getenv("TRANSIFEX_API_TOKEN")
    if not api_token:
        parser = ConfigParser()
        parser.read(os.path.expanduser("~/.transifexrc"))
        if parser.has_section("https://www.transifex.com"):
            api_token = parser.get("https://www.transifex.com", "token")

    if not api_token:
        logger.error("Please define the TRANSIFEX_API_TOKEN env var.")
        sys.exit(1)

    headers = {"Authorization": f"Bearer {api_token}"}
    base_url = "https://rest.api.transifex.com"
    base_params = {"filter[project]": "o:django:p:django"}

    resources_url = base_url + "/resources"
    resource_stats_url = base_url + "/resource_language_stats"

    response = requests.get(resources_url, headers=headers, params=base_params)
    response.raise_for_status()
    data = response.json()["data"]

    for item in data:
        if item["type"] != "resources":
            continue
        resource_id = item["id"]
        resource_name = item["attributes"]["name"]
        params = base_params.copy()
        params.update({"filter[resource]": resource_id})
        stats = requests.get(resource_stats_url, headers=headers, params=params)
        stats_data = stats.json()["data"]
        for lang_data in stats_data:
            lang_id = lang_data["id"].split(":")[-1]
            lang_attributes = lang_data["attributes"]
            last_update = lang_attributes["last_translation_update"]
            if verbose:
                logger.info(
                    f"CHECKING {resource_name} for {lang_id=} updated on {last_update}"
                )
            if last_update is None:
                resource_lang_unchanged[resource_name].append(lang_id)
                continue

            last_update = datetime.strptime(last_update, "%Y-%m-%dT%H:%M:%SZ")
            if last_update > date_since and (
                date_skip is None or last_update.date() != date_skip.date()
            ):
                if verbose:
                    logger.info(f"=> CHANGED {lang_attributes=} {date_skip=}")
                resource_lang_changed[resource_name].append(lang_id)
            else:
                resource_lang_unchanged[resource_name].append(lang_id)

    if verbose:
        unchanged = "\n".join(
            f"\n * resource {res} languages {' '.join(sorted(langs))}"
            for res, langs in resource_lang_unchanged.items()
        )
        logger.info(f"== SUMMARY for unchanged resources ==\n{unchanged}")

    return resource_lang_changed

def _get_locale_dirs(resources, include_core=True):
    """
    Return a tuple (contrib name, absolute path) for all locale directories,
    optionally including the django core catalog.
    If resources list is not None, filter directories matching resources content.
    """
    contrib_dir = os.path.join(os.getcwd(), "django", "contrib")
    dirs = []

    # Collect all locale directories
    for contrib_name in os.listdir(contrib_dir):
        path = os.path.join(contrib_dir, contrib_name, "locale")
        if os.path.isdir(path):
            dirs.append((contrib_name, path))
            if contrib_name in HAVE_JS:
                dirs.append((f"{contrib_name}-js", path))
    if include_core:
        dirs.insert(0, ("core", os.path.join(os.getcwd(), "django", "conf", "locale")))

    # Filter by resources, if any
    if resources is not None:
        res_names = [d[0] for d in dirs]
        dirs = [ld for ld in dirs if ld[0] in resources]
        if len(resources) > len(dirs):
            logger.error(
                "You have specified some unknown resources. "
                "Available resource names are: %s" % (", ".join(res_names),)
            )
            sys.exit(1)
    return dirs

def _tx_resource_for_name(name):
    """Return the Transifex resource name"""
    return "django.core" if name == "core" else f"django.contrib-{name}"

def _check_diff(cat_name, base_path):
    """
    Output the approximate number of changed/added strings in the en catalog.
    """
    po_path = os.path.join(base_path, "en", "LC_MESSAGES", f"django{'js' if cat_name.endswith('-js') else ''}.po")
    try:
        result = run(
            ["git", "diff", "-U0", po_path], capture_output=True, check=True, text=True
        )
        num_changes = result.stdout.count("msgid")
        logger.info(f"{num_changes} changed/added messages in '{cat_name}' catalog.")
    except CalledProcessError as e:
        logger.error(f"Error occurred while checking diffs for {cat_name}: {e}")
        sys.exit(1)

# Other functions remain largely the same

if __name__ == "__main__":
    parser = ArgumentParser()

    subparsers = parser.add_subparsers(
        dest="cmd", help="choose the operation to perform"
    )

    parser_update = subparsers.add_parser(
        "update_catalogs",
        help="update English django.po files with new/updated translatable strings",
    )
    add_common_arguments(parser_update)

    parser_stats = subparsers.add_parser(
        "lang_stats",
        help="print the approximate number of changed/added strings in the en catalog",
    )
    add_common_arguments(parser_stats)

    parser_fetch = subparsers.add_parser(
        "fetch",
        help="fetch translations from Transifex, wrap long lines, generate mo files",
    )
    add_common_arguments(parser_fetch)

    parser_fetch = subparsers.add_parser(
        "fetch_since",
        help=(
            "fetch translations from Transifex modified since a given date "
            "(for all languages and all resources)"
        ),
    )
    parser_fetch.add_argument("-v", "--verbose", action="store_true")
    parser_fetch.add_argument(
        "-s",
        "--since",
        required=True,
        dest="date_since",
        metavar="YYYY-MM-DD",
        type=datetime.fromisoformat,
        help="fetch new translations since this date (ISO format YYYY-MM-DD).",
    )
    parser_fetch.add_argument(
        "--skip",
        dest="date_skip",
        metavar="YYYY-MM-DD",
        type=datetime.fromisoformat,
        help="skip changes from this date (ISO format YYYY-MM-DD).",
    )
    parser_fetch.add_argument("--dry-run", dest="dry_run", action="store_true")

    options = parser.parse_args()
    kwargs = vars(options)
    cmd = kwargs.pop("cmd")

    if cmd:
        eval(cmd)(**kwargs)
    else:
        parser.print_help()
        sys.exit(1)
