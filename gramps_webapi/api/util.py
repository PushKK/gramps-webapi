#
# Gramps Web API - A RESTful API for the Gramps genealogy program
#
# Copyright (C) 2020-2022      David Straub
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#

"""Utility functions."""

import hashlib
import io
import os
import smtplib
import socket
from email.message import EmailMessage
from email.utils import make_msgid
from http import HTTPStatus
from typing import BinaryIO, List, Optional, Sequence, Tuple

from flask import abort, current_app, g, jsonify, make_response, request
from flask_jwt_extended import get_jwt
from gramps.cli.clidbman import NAME_FILE, CLIDbManager
from gramps.gen.config import config
from gramps.gen.const import GRAMPS_LOCALE
from gramps.gen.db.base import DbReadBase
from gramps.gen.db.dbconst import DBBACKEND
from gramps.gen.dbstate import DbState
from gramps.gen.errors import HandleError
from gramps.gen.proxy import PrivateProxyDb
from gramps.gen.utils.grampslocale import GrampsLocale
from marshmallow import RAISE
from webargs.flaskparser import FlaskParser
from werkzeug.security import safe_join

from ..auth import config_get, get_tree, get_tree_usage, set_tree_usage
from ..auth.const import PERM_VIEW_PRIVATE
from ..const import DB_CONFIG_ALLOWED_KEYS, LOCALE_MAP, TREE_MULTI
from ..dbmanager import WebDbManager
from .auth import has_permissions
from .search import SearchIndexer


class Parser(FlaskParser):
    # raise in case of unknown query arguments
    DEFAULT_UNKNOWN_BY_LOCATION = {"query": RAISE}

    def handle_error(self, error, req, schema, *, error_status_code, error_headers):
        status_code = error_status_code or self.DEFAULT_VALIDATION_STATUS
        abort(
            make_response(jsonify(error.messages), status_code),
            exc=error,
            messages=error.messages,
            schema=schema,
            headers=error_headers,
        )


parser = Parser()
use_args = parser.use_args


class ModifiedPrivateProxyDb(PrivateProxyDb):
    """PrivateProxyDb with additional methods."""

    def __init__(self, *args, **kwargs):
        """Initialize self."""
        super().__init__(*args, **kwargs)
        self.name_formats = self.db.name_formats

    def get_dbname(self):
        """Get the name of the database."""
        return self.db.get_dbname()

    def get_summary(self):
        """Return dictionary of summary item."""
        return self.db.get_summary()

    def get_surname_list(self):
        """Return the list of locale-sorted surnames contained in the database."""
        return self.db.get_surname_list()

    def get_place_types(self):
        """Return custom place types assocated with places in the database."""
        return self.db.get_place_types()

    def set_name_group_mapping(self, name, group):
        """Set a name group mapping."""
        return self.db.set_name_group_mapping(name, group)


def get_db_manager(tree: Optional[str]) -> WebDbManager:
    """Get an appropriate WebDbManager instance."""
    return WebDbManager(
        dirname=tree,
        username=current_app.config["POSTGRES_USER"],
        password=current_app.config["POSTGRES_PASSWORD"],
        create_if_missing=False,
    )


def get_tree_from_jwt() -> Optional[str]:
    """Get the tree ID from the token.

    Needs request context.
    """
    claims = get_jwt()
    tree = claims.get("tree")
    return tree


def get_db_outside_request(tree: str, view_private: bool, readonly: bool) -> DbReadBase:
    """Open the database and get the current instance.

    Called before every request.

    If a user is not authorized to view private records,
    returns a proxy DB instance.

    If `readonly` is false, locks the database during the request.
    """
    dbmgr = get_db_manager(tree)
    dbstate = dbmgr.get_db(readonly=readonly)
    if not view_private:
        if not readonly:
            # requesting write access on a private proxy DB is impossible & forbidden!
            abort(HTTPStatus.FORBIDDEN)
        # if we're not authorized to view private records,
        # return a proxy DB instead of the real one
        return ModifiedPrivateProxyDb(dbstate.db)
    return dbstate.db


def get_db_handle(readonly: bool = True) -> DbReadBase:
    """Open the database and get the current instance.

    Called before every request.

    If a user is not authorized to view private records,
    returns a proxy DB instance.

    If `readonly` is false, locks the database during the request.
    """
    view_private = has_permissions({PERM_VIEW_PRIVATE})
    tree = get_tree_from_jwt()

    if readonly and "db" not in g:
        # cache the db instance for the duration of
        # the request
        db = get_db_outside_request(
            tree=tree,
            view_private=view_private,
            readonly=True,
        )
        g.db = db

    if not view_private:
        if not readonly:
            # requesting write access on a private proxy DB is impossible & forbidden!
            abort(HTTPStatus.FORBIDDEN)
        return ModifiedPrivateProxyDb(g.db)

    if not readonly and "db_write" not in g:
        # cache the DbState instance for the duration of
        # the request
        # cache the db instance for the duration of
        # the request
        db_write = get_db_outside_request(
            tree=tree,
            view_private=view_private,
            readonly=False,
        )
        g.db_write = db_write
    if not readonly:
        return g.db_write
    return g.db


def get_search_indexer(tree: str) -> SearchIndexer:
    """Get the search indexer for the tree."""
    base_dir = current_app.config["SEARCH_INDEX_DIR"]
    index_dir = os.path.join(base_dir, tree)
    return SearchIndexer(index_dir=index_dir)


def get_locale_for_language(language: str, default: bool = False) -> GrampsLocale:
    """Get GrampsLocale set to specified language."""
    if language is not None:
        catalog = GRAMPS_LOCALE.get_language_dict()
        for entry in catalog:
            if catalog[entry] == language:
                # translate language code (e.g. "da") to locale code (e.g. "da_DK")
                locale_code = LOCALE_MAP.get(language, language)
                if "UTF" not in locale_code.upper():
                    locale_code = f"{locale_code}.UTF-8"
                return GrampsLocale(lang=locale_code)
    if default:
        return GRAMPS_LOCALE
    return None


def get_buffer_for_file(filename: str, delete=True, not_found=False) -> BinaryIO:
    """Return binary buffer with file contents."""
    try:
        with open(filename, "rb") as file_handle:
            buffer = io.BytesIO(file_handle.read())
    except FileNotFoundError:
        if not_found:
            raise FileNotFoundError
        abort(500)
    if delete:
        os.remove(filename)
    return buffer


def send_email(
    subject: str, body: str, to: Sequence[str], from_email: Optional[str] = None
) -> None:
    """Send an e-mail message."""
    msg = EmailMessage()
    msg.set_content(body)
    msg["Subject"] = subject
    if not from_email:
        from_email = get_config("DEFAULT_FROM_EMAIL")
    msg["From"] = from_email
    msg["To"] = ", ".join(to)
    msg["Message-ID"] = make_msgid()

    host = get_config("EMAIL_HOST")
    port = int(get_config("EMAIL_PORT"))
    user = get_config("EMAIL_HOST_USER")
    password = get_config("EMAIL_HOST_PASSWORD")
    use_tls = get_config("EMAIL_USE_TLS")
    try:
        if use_tls:
            smtp = smtplib.SMTP_SSL(host=host, port=port, timeout=10)
        else:
            smtp = smtplib.SMTP(host=host, port=port, timeout=10)
            smtp.ehlo()
            if port != 25:
                smtp.starttls()
                smtp.ehlo()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)
        smtp.quit()
    except ConnectionRefusedError:
        current_app.logger.error("Connection to SMTP server refused.")
        raise ValueError("Connection was refused.")
    except socket.timeout:
        current_app.logger.error("SMTP connection attempt timed out.")
        raise ValueError("Connection attempt timed out.")
    except OSError:
        current_app.logger.error("Error while trying to send e-mail.")
        raise ValueError("Error while trying to send e-mail.")


def make_cache_key_thumbnails(*args, **kwargs):
    """Make a cache key for thumbnails."""
    # hash query args except jwt
    query_args = list((k, v) for (k, v) in request.args.items(multi=True) if k != "jwt")
    args_as_sorted_tuple = tuple(sorted(query_args))
    args_as_bytes = str(args_as_sorted_tuple).encode()
    arg_hash = hashlib.md5(args_as_bytes)
    arg_hash = str(arg_hash.hexdigest())

    # get media checksum
    handle = kwargs["handle"]
    tree = get_tree_from_jwt()
    db_handle = get_db_handle()
    try:
        obj = db_handle.get_media_from_handle(handle)
    except HandleError:
        abort(404)
    # checksum in the DB
    checksum = obj.checksum

    dbmgr = get_db_manager(tree)

    cache_key = checksum + request.path + arg_hash + dbmgr.dirname

    return cache_key


def get_config(key: str) -> Optional[str]:
    """Get a config item.

    If exists, returns the config item from the database.
    Else, uses the app.config dictionary.
    """
    if key in DB_CONFIG_ALLOWED_KEYS:
        val = config_get(key)
        if val is not None:
            return val
    return current_app.config.get(key)


def list_trees() -> List[Tuple[str, str]]:
    """Get a list of tree dirnames and names."""
    dbstate = DbState()
    dbman = CLIDbManager(dbstate)
    return dbman.current_names


def get_tree_id(guid: str) -> str:
    """Get the appropriate tree ID for a user."""
    tree_id = get_tree(guid)
    if not tree_id:
        if current_app.config["TREE"] == TREE_MULTI:
            # multi-tree support enabled but user has no tree ID: forbidden!
            abort(403)
        # needed for backwards compatibility: single-tree mode but user without tree ID
        dbmgr = WebDbManager(name=current_app.config["TREE"], create_if_missing=False)
        tree_id = dbmgr.dirname
    return tree_id


def tree_exists(tree_id: str) -> bool:
    """Check if a tree exists."""
    dbdir = config.get("database.path")
    dir_path = safe_join(dbdir, tree_id)
    if not dir_path:
        return False
    if not os.path.isdir(dir_path):
        return False
    name_path = os.path.join(dir_path, NAME_FILE)
    if not os.path.isfile(name_path):
        return False
    dbid_path = os.path.join(dir_path, DBBACKEND)
    if not os.path.isfile(dbid_path):
        return False
    return True


def update_usage_people() -> int:
    """Update the usage of people."""
    tree = get_tree_from_jwt()
    db_handle = get_db_handle()
    usage_people = db_handle.get_number_of_people()
    set_tree_usage(tree, usage_people=usage_people)
    return usage_people


def check_quota_people(to_add: int) -> None:
    """Check whether the quota allows adding `to_add` people and abort if not."""
    tree = get_tree_from_jwt()
    usage_dict = get_tree_usage(tree)
    if not usage_dict or usage_dict.get("usage_people") is None:
        update_usage_people()
    usage_dict = get_tree_usage(tree)
    usage = usage_dict["usage_people"]
    quota = usage_dict.get("quota_people")
    if quota is None:
        return
    if usage + to_add > quota:
        abort(405)
