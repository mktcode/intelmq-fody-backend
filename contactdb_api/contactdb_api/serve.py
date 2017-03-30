#!/usr/bin/env python3
"""Serve intelmq-certbund-contact db api via wsgi.

Requires hug (http://www.hug.rest/)

Copyright (C) 2017 by Bundesamt für Sicherheit in der Informationstechnik

Software engineering by Intevation GmbH

This program is Free Software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.

Author(s):
    * Bernhard E. Reiter <bernhard@intevation.de>


Design rationale:
    Our services shall be accessed by
    https://github.com/Intevation/intelmq-fody
    so our "endpoints" should be reachable from the same ip:port as
    the checkticket endpoints.

    We need location and credentials for the database holding the contactdb.
    serve.py [1] (a hug based backend) solves this problem by reusing
    the intelmq-mailgen configuration to access the 'intelmq-events' database.
    This serving part need to access a different database 'contactdb', thus
    we start with our on configuration.

    [1] https://github.com/Intevation/intelmq-mailgen/blob/master/extras/checkticket_api/serve.py # noqa

"""
import json
import logging
import os
import sys
# FUTURE the typing module is part of Python's standard lib for v>=3.5
# try:
#     from typing import Tuple, Union, Sequence, List
# except:
#     pass

from falcon import HTTP_BAD_REQUEST, HTTP_NOT_FOUND
import hug
import psycopg2
from psycopg2.extras import RealDictCursor

log = logging.getLogger(__name__)
# adding a custom log level for even more details when diagnosing
DD = logging.DEBUG-2
logging.addLevelName(DD, "DDEBUG")


def read_configuration() -> dict:
    """Read configuration file.

    If the environment variable CONTACTDB_SERVE_CONF_FILE exist, use it
    for the file name. Otherwise uses a default.

    Returns:
        The configuration values, possibly containing more dicts.

    Notes:
      Design rationale:
        * Provies an "okay" separation from config and code.
        * Better than intelmq-mailgen which has two hard-coded places
          and merge code for the config.
        * (Inspired by https://12factor.net/config.) But it is not a good
          idea to put credential information in the commandline or environment.
        * We are using json for the configuration file format and not
          Python's configparser module to stay more in line with intelmq's
          overall design philosophy to use json for configuration files.
    """
    config = None
    config_file_name = os.environ.get(
                        "CONTACTDB_SERVE_CONF_FILE",
                        "/etc/intelmq/contactdb-serve.conf")

    if os.path.isfile(config_file_name):
        with open(config_file_name) as config_handle:
                config = json.load(config_handle)

    return config if isinstance(config, dict) else {}


EXAMPLE_CONF_FILE = r"""
{
  "libpg conninfo":
    "host=localhost dbname=contactdb user=apiuser password='USER\\'s DB PASSWORD'",
  "logging_level": "INFO"
}
""" # noqa

ENDPOINT_PREFIX = '/api/contactdb'
ENDPOINT_NAME = 'ContactDB'


class Error(Exception):
    """Base class for exceptions in this module."""
    pass


class CommitError(Error):
    """Exception raises if a commit action fails.
    """
    pass


# Using a global object for the database connection
# must be initialised once
contactdb_conn = None


def open_db_connection(dsn: str):
    global contactdb_conn

    contactdb_conn = psycopg2.connect(dsn=dsn)
    return contactdb_conn


def __commit_transaction():
    global contactdb_conn
    log.log(DD, "Calling commit()")
    contactdb_conn.commit()


def __rollback_transaction():
    global contactdb_conn
    log.log(DD, "Calling rollback()")
    contactdb_conn.rollback()


# FUTURE once typing is available
# def _db_query(operation:str, parameters:Union[dict, list]=None,
#              end_transaction:bool=True) -> Tuple(list, list):
def _db_query(operation: str, parameters=None, end_transaction: bool=True):
    """Does an database query.

    Creates a cursor from the global database connection, runs
    the query or command the fetches all results.

    Parameters:
        operation: The query to be used by psycopg2.cursor.execute()
        parameters: for the sql query
        end_transaction: set to False to do subsequent queries in the same
            transaction.

    Returns:
        Tuple[list, List[psycopg2.extras.RealDictRow]]:
            description and results.
    """
    global contactdb_conn

    description = None

    # pscopgy2.4 does not offer 'with' for cursor()
    # FUTURE use with
    cur = contactdb_conn.cursor(cursor_factory=RealDictCursor)

    cur.execute(operation, parameters)
    log.log(DD, "Ran query={}".format(repr(cur.query.decode('utf-8'))))
    description = cur.description
    results = cur.fetchall()

    if end_transaction:
        __commit_transaction()

    cur.close()

    return (description, results)


def _db_manipulate(operation: str, parameters=None,
                   end_transaction: bool=True) -> int:
    """Manipulates the database.

    Creates a cursor from the global database connection, runs the command.

    Parameters:
        operation: The query to be used by psycopg2.cursor.execute()
        parameters: for the sql query
        end_transaction: set to False to do subsequent queries in the same
            transaction.

    Returns:
        Number of affected rows.
    """
    global contactdb_conn

    # pscopgy2.4 does not offer 'with' for cursor()
    # FUTURE use with
    cur = contactdb_conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(operation, parameters)
    log.log(DD, "Ran query={}".format(cur.query.decode('utf-8')))
    if end_transaction:
        __commit_transaction()
    cur.close()

    return cur.rowcount


def __db_query_organisation_ids(operation_str: str,  parameters=None):
    """Inquires organisation_ids for a specific query.

    Parameters:
        operation(str): must be a psycopg2 execute operation string that
            only returns an array of ids "AS organisation_ids" or nothing
            it has to contain '{0}' format placeholders for the table variants

    Returns:
        Dict("auto":list, "manual":list): lists of organisation_ids that
            where manually entered or imported automatically
    """
    orgs = {}

    description, results = _db_query(operation_str.format(""), parameters)
    if len(results) == 1 and results[0]["organisation_ids"] is not None:
        orgs["manual"] = results[0]["organisation_ids"]
    else:
        orgs["manual"] = []

    description, results = _db_query(operation_str.format("_automatic"),
                                     parameters)
    if len(results) == 1 and results[0]["organisation_ids"] is not None:
        orgs["auto"] = results[0]["organisation_ids"]
    else:
        orgs["auto"] = []

    return orgs


def __db_query_org(org_id: int, table_variant: str,
                   end_transaction: bool=True) -> dict:
    """Returns details for an organisaion.

    Parameters:
        org_id:int: the organisation id to be queried
        table_variant: either "" or "_automatic"

    Returns:
        containing the organisation and additional keys
            'annotations', 'asns' (with 'annotations') and 'contacts'
    """

    operation_str = """
        SELECT * FROM organisation{0} WHERE organisation{0}_id = %s
        """.format(table_variant)

    description, results = _db_query(operation_str, (org_id,), False)

    if not len(results) == 1:
            return {}
    else:
        org = results[0]
        if table_variant != '':  # keep plain id name for all table variants
            org["organisation_id"] = org.pop(
                    "organisation{0}_id".format(table_variant)
                    )

        # insert asns.
        # HINT: we are not using __db_query_asn() because we don't know the
        #   asns yet, so we'll have to do another query anyway and using
        #   the function to encapsulate adding of the annotations would make
        #   the code here less elegant.
        operation_str = """
            SELECT * FROM organisation_to_asn{0}
                WHERE organisation{0}_id = %s
            """.format(table_variant)

        description, results = _db_query(operation_str, (org_id,), False)
        org["asns"] = results

        # insert contacts
        operation_str = """
            SELECT * FROM contact{0}
                WHERE organisation{0}_id = %s
            """.format(table_variant)

        description, results = _db_query(operation_str, (org_id,),
                                         end_transaction)
        org["contacts"] = results

        # insert national certs
        operation_str = """
            SELECT * FROM national_cert{0}
                WHERE organisation{0}_id = %s
            """.format(table_variant)

        description, results = _db_query(operation_str, (org_id,),
                                         end_transaction)
        org["nationalcerts"] = results

        # insert networks
        operation_str = """
            SELECT * FROM network{0} AS n
                JOIN organisation_to_network{0} AS otn
                    ON n.network{0}_id = otn.network{0}_id
                WHERE otn.organisation{0}_id = %s
            """.format(table_variant)

        description, results = _db_query(operation_str, (org_id,),
                                         end_transaction)
        org["networks"] = results

        # insert fqdns
        operation_str = """
            SELECT * FROM fqdn{0} AS f
                JOIN organisation_to_fqdn{0} AS of
                    ON f.fqdn{0}_id = of.fqdn{0}_id
                WHERE of.organisation{0}_id = %s
            """.format(table_variant)

        description, results = _db_query(operation_str, (org_id,),
                                         end_transaction)
        org["fqdns"] = results

        # add existing annotations
        # can only be there for manual tables
        if table_variant == '':
            # insert annotations for the org
            operation_str = """
                SELECT array_agg(annotation) AS annotations
                    FROM organisation_annotation
                    WHERE organisation_id = %s
                """
            description, results = _db_query(operation_str, (org_id,),
                                             end_transaction)
            org["annotations"] = results[0]["annotations"]

            # insert annotations for each asn
            for index, asn in enumerate(org["asns"][:]):
                operation_str = """
                    SELECT * from autonomous_system_annotation
                        WHERE asn = %s
                """
                description, results = _db_query(operation_str,
                                                 (asn["asn"],),
                                                 end_transaction)
                if len(results) > 0:
                    org["asns"][index]["annotations"] = \
                        __db_query_asn_annotations(asn["asn"])

        return org


def __db_query_asn_annotations(asn: int, end_transaction: bool=True) -> list:
    """Queries the annotations for an asn.

    Returns:
        all annotations, even if one occurs several times
    """
    operation_str = """
        SELECT array_agg(annotation) FROM autonomous_system_annotation
            WHERE asn = %s
    """
    description, results = _db_query(operation_str, (asn,), end_transaction)
    return results[0]["array_agg"]


def __db_query_asn(asn: int, table_variant: str,
                   end_transaction: bool=True) -> dict:
    """Returns details for an asn."""

    operation_str = """
                SELECT * FROM organisation_to_asn{0}
                    WHERE asn = %s
                """.format(table_variant)
    description, results = _db_query(operation_str, (asn,), end_transaction)

    if len(results) > 0:
        if table_variant == '':  # insert annotations for manual tables
            results[0]['annotations'] = \
                    __db_query_asn_annotations(asn, end_transaction)
        return results[0]
    else:
        return None


def __remove_inhibitions(inhibitions: list) -> None:
    """Removes inhibitions and afterwards stale network entries.

    Assumes that organisation_to_network is not used.
    """
    operation_str = """
        DELETE FROM inhibition WHERE id = ANY(%s)
        """
    _db_manipulate(operation_str, ([i["id"] for i in inhibitions],), False)

    # remove all manual network entries that are unlinked by inhibition
    operation_str = """
        DELETE FROM network AS n
            WHERE n.id NOT IN (
                SELECT i.net_id FROM inhibition as i
                    WHERE n.id = i.net_id
                )
        """
    _db_manipulate(operation_str, end_transaction=False)


def __remove_or_unlink_asns(asns: list, org_id: int) -> None:
    """Removes or unlinks db entries for asns.

    Considers a list of inhibitions in each asn.

    Parameter:
        asns: to be unlinked or removed
        org_id: the organisation to be unlinked from
    """
    for asn in asns:
        asn_id = asn["number"]
        operation_str = """
            DELETE FROM organisation_to_asn AS oa
                WHERE oa.organisation_id = %s
                  AND oa.asn_id = %s
            """
        _db_manipulate(operation_str, (org_id, asn_id), False)

        # how many connections are left to this asn?
        operation_str = """
            SELECT count(*) FROM organisation_to_asn WHERE asn_id = %s
            """
        description, results = _db_query(operation_str, (asn_id,), False)

        if results[0]["count"] == 0:
            # delete asn, because there is no connection anymore

            asn_in_db = __db_query_asn(asn_id, "", False)

            # ignore in the comparison, because it comes from the n-to-m table
            del(asn["notification_interval"])
            del(asn["organisation_id"])
            del(asn["asn_id"])
            if "inhibitions" in asn:
                __remove_inhibitions(asn["inhibitions"])
                del(asn["inhibitions"])  # comes from inserted inhibitions

            if asn_in_db == asn:
                operation_str = """
                    DELETE FROM autonomous_system
                      WHERE number = %s
                    """
                _db_manipulate(operation_str, (asn_id,), False)
            else:
                log.debug("asn_in_db = {}; asn = {}".format(
                            repr(asn_in_db), repr(asn)))
                raise CommitError("ASN{} to be deleted differs from db entry."
                                  "".format(asn_id))


def __fix_asns_to_org(asns: list, org_id: int) -> None:
    """Make sure that asns with annotations exits and are linked.

    For each asn:
        Add missing annotations
        Remove superfluous ones

        Check the link to the org and create if necessary

    Parameters:
        asns: that should be exist afterwards
        org_id: the org for the asns
    """
    for asn in asns:
        asn_id = asn["asn"]
        annos_should = asn["annotations"]
        log.log(DD, "annos_should = " + repr(annos_should))

        annos_are = __db_query_asn_annotations(asn_id, False)
        log.log(DD, "annos_are = " + repr(annos_are))

        # add missing annotations
        for anno in [a for a in annos_should if a not in annos_are]:
            operation_str = """
                INSERT INTO autonomous_system_annotation
                    (asn, annotation) VALUES (%s, %s)
            """
            _db_manipulate(operation_str, (asn_id, anno), False)

        # remove superfluous annotations
        for anno in [a for a in annos_are if a not in annos_should]:
            operation_str = """
                DELETE FROM autonomous_system_annotation
                    WHERE asn = %s AND annotation = %s
            """
            _db_manipulate(operation_str, (asn_id, anno), False)

        # check linking to the org
        operation_str = """
            SELECT * FROM organisation_to_asn
                WHERE organisation_id = %s AND asn = %s
            """
        description, results = _db_query(operation_str,
                                         (org_id, asn_id,), False)
        if len(results) == 0:
            # add link
            operation_str = """
                INSERT INTO organisation_to_asn
                    (organisation_id, asn) VALUES (%s, %s)
                """
            _db_manipulate(operation_str, (org_id, asn_id), False)

    # remove links between asns and org that should not be there anymore
    operation_str = """
        DELETE FROM organisation_to_asn
            WHERE organisation_id = %s
            AND asn != ALL(%s)
    """
    _db_manipulate(operation_str, (org_id, [asn["asn"] for asn in asns]),
                   end_transaction=False)

    # remove all annotations that are not linked to anymore
    operation_str = """
        DELETE FROM autonomous_system_annotation as asa
            WHERE asa.asn NOT IN (SELECT asn FROM organisation_to_asn)
        """
    _db_manipulate(operation_str, end_transaction=False)


def __remove_or_unlink_contacts(contacts: list, org_id: int) -> None:
    """Removes or unlinks db entries for contacts.

    Parameter:
        contacts: to be unlinked or removed
        org_id: the organisation to be unlinked from
    """
    for contact in contacts:
        contact_id = contact["contact_id"]
        operation_str = """
            DELETE FROM role
                WHERE organisation_id = %s
                  AND contact_id = %s
            """
        _db_manipulate(operation_str, (org_id, contact_id), False)

        # how many connection are left to this contact?
        operation_str = """SELECT count(*) FROM role WHERE contact_id = %s"""
        description, results = _db_query(operation_str, (contact_id,), False)

        if results[0]["count"] == 0:
            # delete contact, because there is no connection anymore

            operation_str = "DELETE FROM contact WHERE id = %s"
            _db_manipulate(operation_str, (contact_id,), False)


def __fix_contacts_to_org(contacts: list, org_id: int) -> None:
    """Make sure that contacts exist and link to the org.
    """
    needed_attribs = ['firstname', 'lastname', 'tel', 'openpgp_fpr',
                      'email', 'comment']

    # first delete all contacts for the org
    operation_str = """
        DELETE FROM contact
            WHERE organisation_id = %s
        """
    _db_manipulate(operation_str, (org_id,), False)

    # then recreate all that we want to have now
    for contact in contacts:
        # we need make sure that all values are there and at least ''
        # as None would be translated to '= NULL' which always fails in SQL
        for attrib in needed_attribs:
            if (attrib not in contact) or contact[attrib] is None:
                raise CommitError("{} not set".format(attrib))

        contact["organisation_id"] = org_id

        operation_str = """
            INSERT INTO contact
                (firstname, lastname, tel,
                 openpgp_fpr, email, comment, organisation_id)
                VALUES (%(firstname)s, %(lastname)s, %(tel)s,
                        %(openpgp_fpr)s, %(email)s, %(comment)s,
                        %(organisation_id)s)
            """
        _db_manipulate(operation_str, contact, False)


def _create_org(org: dict) -> int:
    """Insert an new contactdb entry.

    Makes sure that the contactdb entry expressed by the org dict
    is in the tables for manual entries.

    First checks the linked asns and linked contact tables.
    Then checks the organisation itself.
    Afterwards checks the n-to-m entries that link the tables.

    Checks for each query if an entry with equal values is already in the
    table. If so, uses the existing entry, otherwise inserts a new entry.

    Returns:
        Database ID of the organisation that has been there or was created.
    """
    log.debug("_create_org called with " + repr(org))

    needed_attribs = ['name', 'comment', 'ripe_org_hdl',
                      'ti_handle', 'first_handle']

    for attrib in needed_attribs:
        if attrib in org:
            if org[attrib] is None:
                org[attrib] = ''
        else:
            raise CommitError("{} not set".format(attrib))

    if org['name'] == '':
        raise CommitError("Name of the organisation must be provided.")

    operation_str = """
        SELECT organisation_id FROM organisation as o
            WHERE o.name = %(name)s
              AND o.comment = %(comment)s
              AND o.ripe_org_hdl = %(ripe_org_hdl)s
              AND o.ti_handle = %(ti_handle)s
              AND o.first_handle = %(first_handle)s
        """
    if (('sector_id' not in org) or org['sector_id'] is None
            or org['sector_id'] == ''):
        operation_str += " AND o.sector_id IS NULL"
        org["sector_id"] = None
    else:
        operation_str += " AND o.sector_id = %(sector_id)s"

    description, results = _db_query(operation_str, org, False)
    if len(results) > 1:
        raise CommitError("More than one organisation row like"
                          " {} in the db".format(org))
    elif len(results) == 1:
        new_org_id = results[0]["organisation_id"]
    else:
        operation_str = """
            INSERT INTO organisation
                (name, sector_id, comment, ripe_org_hdl,
                 ti_handle, first_handle)
                VALUES (%(name)s, %(sector_id)s, %(comment)s, %(ripe_org_hdl)s,
                        %(ti_handle)s, %(first_handle)s)
                RETURNING organisation_id
            """
        description, results = _db_query(operation_str, org, False)
        new_org_id = results[0]["organisation_id"]

    __fix_asns_to_org(org['asns'], new_org_id)
    __fix_contacts_to_org(org['contacts'], new_org_id)

    return(new_org_id)


def _update_org(org):
    """Update a contactdb entry.

    First update asns and links to them then the same for contacts.
    Last update of the values of the org itself.

    Returns:
        Database ID of the updated organisation.
    """
    log.debug("_update_org called with " + repr(org))

    org_id = org["id"]
    org_in_db = __db_query_org(org_id, "", end_transaction=False)

    if ("id" not in org_in_db) or org_in_db["id"] != org_id:
        raise CommitError("Org {} to be updated not in db.".format(org_id))

    if 'name' not in org or org['name'] is None or org['name'] == '':
        raise CommitError("Name of the organisation must be provided.")

    __fix_asns_to_org(org["asns"], org_id)
    __fix_contacts_to_org(org["contacts"], org_id)

    if org["sector_id"] == '':
        org["sector_id"] = None

    # linking of asns and contacts has been done, only update is left to do
    operation_str = """
        UPDATE organisation
            SET (name, sector_id, comment, ripe_org_hdl,
                 ti_handle, first_handle)
              = (%(name)s, %(sector_id)s, %(comment)s, %(ripe_org_hdl)s,
                 %(ti_handle)s, %(first_handle)s)
            WHERE id = %(id)s
        """
    _db_manipulate(operation_str, org, False)

    return org_id


def _delete_org(org) -> int:
    """Delete an contactdb entry.

    Also delete the attached asns and contact entries, if they are
    not used elsewhere.

    Returns:
        Database ID of the organisation that has been deleted.
    """
    log.debug("_delete_org called with " + repr(org))

    org_in_db = __db_query_org(org["id"], "", end_transaction=False)

    if not org_in_db == org:
        log.debug("org_in_db = {}; org = {}".format(repr(org_in_db),
                                                    repr(org)))
        raise CommitError("Org to be deleted differs from db entry.")

    __remove_or_unlink_asns(org['asns'], org['id'])
    __remove_or_unlink_contacts(org['contacts'], org['id'])

    # remove org itself
    operation_str = "DELETE FROM organisation WHERE id = %s"
    affected_rows = _db_manipulate(operation_str, (org["id"],), False)

    if affected_rows == 1:
        return org["id"]


@hug.startup()
def setup(api):
    config = read_configuration()
    if "logging_level" in config:
        log.setLevel(config["logging_level"])
    open_db_connection(config["libpg conninfo"])
    log.debug("Initialised DB connection for contactdb_api.")


@hug.get(ENDPOINT_PREFIX + '/ping')
def pong():
    return ["pong"]


@hug.get(ENDPOINT_PREFIX + '/searchasn')
def searchasn(asn: int):
    return __db_query_organisation_ids("""
        SELECT DISTINCT array_agg(organisation{0}_id) as organisation_ids
            FROM organisation_to_asn{0}
            WHERE asn=%s
        """, (asn,))


@hug.get(ENDPOINT_PREFIX + '/searchorg')
def searchorg(name: str):
    """Search for an entry with the given name.

    Search is an case-insensitive substring search.
    """
    return __db_query_organisation_ids("""
        SELECT DISTINCT array_agg(o.organisation{0}_id) AS organisation_ids
            FROM organisation{0} AS o
            WHERE name ILIKE %s
               OR name ILIKE %s
               OR name ILIKE %s
               OR name ILIKE %s
        """, (name, "%"+name+"%", "%"+name, name+"%"))


@hug.get(ENDPOINT_PREFIX + '/searchcontact')
def searchcontact(email: str):
    """Search for an entry with the given email address.

    Uses a case-insensitive substring search.
    """
    return __db_query_organisation_ids("""
        SELECT DISTINCT array_agg(c.organisation{0}_id) AS organisation_ids
            FROM contact{0} AS c
            WHERE c.email LIKE %s
               OR c.email LIKE %s
               OR c.email LIKE %s
               OR c.email LIKE %s
        """, (email, "%"+email+"%", "%"+email, email+"%"))


@hug.get(ENDPOINT_PREFIX + '/org/manual/{id}')
def get_manual_org_details(id: int):
    return __db_query_org(id, "")


@hug.get(ENDPOINT_PREFIX + '/org/auto/{id}')
def get_auto_org_details(id: int):
    return __db_query_org(id, "_automatic")


@hug.get(ENDPOINT_PREFIX + '/asn/manual/{number}')
def get_manual_asn_details(number: int, response):
    asn = __db_query_asn(number, "")

    if asn is None:
        response.status = HTTP_NOT_FOUND
        return {"reason": "ASN not found"}
    else:
        return asn


# a way to test this is similiar to
#   import requests
#   requests.post('http://localhost:8000/api/contactdb/org/manual/commit', json={'one': 'two'}, auth=('user', 'pass')).json() # noqa
@hug.post(ENDPOINT_PREFIX + '/org/manual/commit')
def commit_pending_org_changes(body, response):

    log.info("Got commit_object = " + repr(body))
    if not (body
            and 'commands' in body
            and len(body['commands']) > 0
            and 'orgs' in body
            and len(body['orgs']) > 0
            and len(body['commands']) == len(body['orgs'])):
        response.status = HTTP_BAD_REQUEST
        return {'reason': "Needs commands and orgs arrays of same length."}

    commands = body['commands']
    orgs = body['orgs']

    known_commands = {  # list of commands and function table
        'create': _create_org,
        'update': _update_org,
        'delete': _delete_org
        }

    for command in commands:
        if command not in known_commands:
            response.status = HTTP_BAD_REQUEST
            return {'reason':
                    "Unknown command. Not in " + str(known_commands.keys())}

    results = []
    try:
        for command, org in zip(commands, orgs):
            results.append((command, known_commands[command](org)))
    except Exception as err:
        __rollback_transaction()
        log.info("Commit failed '%s' with '%r'", command, org, exc_info=True)
        response.status = HTTP_BAD_REQUEST
        return {"reason": "Commit failed, see server logs."}
    else:
        __commit_transaction()

    log.info("Commit successful, results = {}".format(results,))
    return results


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--example-conf':
        print(EXAMPLE_CONF_FILE)
        exit()

    config = read_configuration()
    print("config = {}".format(config,))
    if "logging_level" in config:
        log.setLevel(config["logging_level"])

    print("log.name = \"{}\"".format(log.name))
    print("log effective level = \"{}\"".format(
        logging.getLevelName(log.getEffectiveLevel())))

    cur = open_db_connection(config["libpg conninfo"]).cursor()

    for count in [
            "autonomous_system_automatic",
            "autonomous_system",
            "organisation_automatic",
            "organisation",
            "contact_automatic",
            "contact"
            ]:
        cur.execute("SELECT count(*) FROM {}".format(count))
        result = cur.fetchone()
        print("count {} = {}".format(count, result))

    cur.execute("SELECT count(*) FROM autonomous_system")
    cur.connection.commit()  # end transaction
