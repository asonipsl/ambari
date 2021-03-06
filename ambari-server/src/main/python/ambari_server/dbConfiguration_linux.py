#!/usr/bin/env python

'''
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
'''
import fileinput
import glob
import os
import re
import shutil
import socket
import subprocess
import sys
import time

from ambari_commons import OSCheck, OSConst
from ambari_commons.logging_utils import get_silent, get_verbose, print_error_msg, print_info_msg, print_warning_msg
from ambari_commons.exceptions import NonFatalException, FatalException
from ambari_commons.os_utils import copy_files, find_in_path, is_root, remove_file, run_os_command
from ambari_server.dbConfiguration import DBMSConfig, USERNAME_PATTERN, SETUP_DB_CONNECT_ATTEMPTS, \
    SETUP_DB_CONNECT_TIMEOUT, STORAGE_TYPE_LOCAL, DEFAULT_USERNAME, DEFAULT_PASSWORD
from ambari_server.serverConfiguration import get_ambari_properties, get_value_from_properties, configDefaults, \
    OS_TYPE, OS_FAMILY, AMBARI_PROPERTIES_FILE, RESOURCES_DIR_PROPERTY, \
    JDBC_DATABASE_PROPERTY, JDBC_DATABASE_NAME_PROPERTY, JDBC_POSTGRES_SCHEMA_PROPERTY, \
    JDBC_HOSTNAME_PROPERTY, JDBC_PORT_PROPERTY, \
    JDBC_USER_NAME_PROPERTY, JDBC_PASSWORD_PROPERTY, JDBC_PASSWORD_FILENAME, \
    JDBC_DRIVER_PROPERTY, JDBC_URL_PROPERTY, \
    JDBC_RCA_USER_NAME_PROPERTY, JDBC_RCA_PASSWORD_ALIAS, JDBC_RCA_PASSWORD_FILE_PROPERTY, \
    JDBC_RCA_DRIVER_PROPERTY, JDBC_RCA_URL_PROPERTY, \
    PERSISTENCE_TYPE_PROPERTY, encrypt_password, store_password_file
from ambari_server.userInput import get_YN_input, get_validated_string_input, read_password
from ambari_server.utils import get_postgre_hba_dir, get_postgre_running_status

ORACLE_DB_ID_TYPES = ["Service Name", "SID"]
ORACLE_SNAME_PATTERN = "jdbc:oracle:thin:@.+:.+:.+"

JDBC_PROPERTIES_PREFIX = "server.jdbc.properties."

class LinuxDBMSConfig(DBMSConfig):
  def __init__(self, options, properties, storage_type):
    super(LinuxDBMSConfig, self).__init__(options, properties, storage_type)

    #Init the database configuration data here, if any
    self.dbms_full_name = ""

    # The values from options supersede the values from properties
    self.database_host = DBMSConfig._init_member_with_prop_default(options, "database_host",
                                                                   properties, JDBC_HOSTNAME_PROPERTY, "localhost")
    #self.database_port is set in the subclasses
    self.database_name = DBMSConfig._init_member_with_prop_default(options, "database_name",
                                                                   properties, JDBC_DATABASE_NAME_PROPERTY, configDefaults.DEFAULT_DB_NAME)

    self.database_username = DBMSConfig._init_member_with_prop_default(options, "database_username",
                                                                       properties, JDBC_USER_NAME_PROPERTY, DEFAULT_USERNAME)
    self.database_password = getattr(options, "database_password", "")
    if not self.database_password:
      self.database_password = DBMSConfig._read_password_from_properties(properties)

    self.database_url_pattern = ""
    self.database_url_pattern_alt = ""

    self.database_storage_name = ""
    self.sid_or_sname = "sname"

    self.init_script_file = ""
    self.drop_tables_script_file = ""
    self.client_tool_usage_pattern = ""

    self.jdbc_extra_params = []

  def _prompt_db_properties(self):
    if self.must_set_database_options:
      if self.persistence_type != STORAGE_TYPE_LOCAL:
        self.database_host = get_validated_string_input(
            "Hostname (" + self.database_host + "): ",
            self.database_host,
            "^[a-zA-Z0-9.\-]*$",
            "Invalid hostname.",
            False
        )

        self.database_port = get_validated_string_input(
            "Port (" + self.database_port + "): ",
            self.database_port,
            "^([0-9]{1,4}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5])$",
            "Invalid port.",
            False
        )

      if not self._configure_database_name():
        return False

      # Username is common for Oracle/MySQL/Postgres
      self.database_username = get_validated_string_input(
          'Username (' + self.database_username + '): ',
          self.database_username,
          USERNAME_PATTERN,
          "Invalid characters in username. Start with _ or alpha "
          "followed by alphanumeric or _ or - characters",
          False
      )
      self.database_password = LinuxDBMSConfig._configure_database_password(True)

    self._display_db_properties()
    return True

  # Supporting remote server for all the DB types. Supporting local server only for PostgreSQL.
  def _setup_remote_server(self, args):
    self._store_remote_properties(args)

  def _setup_remote_database(self):
    properties = get_ambari_properties()
    if properties == -1:
      err = 'Error getting ambari properties'
      print_error_msg(err)
      raise FatalException(-1, err)

    if self.ensure_jdbc_driver_installed(properties):
      print 'Configuring remote database connection properties...'
      retcode = self._setup_remote_db()
      if retcode == -1:
        err = "Remote database setup aborted."
        raise NonFatalException(err)
      if not retcode == 0:
        err = 'Error while configuring connection properties. Exiting'
        raise FatalException(retcode, err)

  def _reset_remote_database(self):
    client_usage_cmd_drop = self._get_remote_script_line(self.drop_tables_script_file)
    client_usage_cmd_init = self._get_remote_script_line(self.init_script_file)

    print_warning_msg('To reset Ambari Server schema ' +
                      'you must run the following DDL against the database to '
                      + 'drop the schema:' + os.linesep + client_usage_cmd_drop
                      + os.linesep + 'Then you must run the following DDL ' +
                      'against the database to create the schema: ' + os.linesep +
                      client_usage_cmd_init + os.linesep)

  def _install_jdbc_driver(self, properties, files_list):
    if type(files_list) is not int:
      print 'Copying JDBC drivers to server resources...'
      try:
        resources_dir = properties[RESOURCES_DIR_PROPERTY]
      except KeyError:
        print_error_msg("There is no value for " + RESOURCES_DIR_PROPERTY + "in " + AMBARI_PROPERTIES_FILE)
        return False

      db_name = self.dbms_full_name.lower()
      symlink_name = db_name + "-jdbc-driver.jar"
      jdbc_symlink = os.path.join(resources_dir, symlink_name)
      db_default_driver_path = os.path.join(configDefaults.JAVA_SHARE_PATH, self.driver_file_name)

      if os.path.lexists(jdbc_symlink):
        os.remove(jdbc_symlink)

      copy_status = copy_files(files_list, resources_dir)

      if not copy_status == 0:
        raise FatalException(-1, "Failed to copy JDBC drivers to server resources")

      if db_default_driver_path in files_list:
        os.symlink(os.path.join(resources_dir, self.driver_file_name), jdbc_symlink)
    else:
      if files_list == -1:
        return False
    return True

  def _configure_database_name(self):
    return True

  def _get_remote_script_line(self, scriptFile):
    return None

  @staticmethod
  def _configure_database_password(showDefault=True):
    passwordDefault = DEFAULT_PASSWORD
    if showDefault:
      passwordPrompt = 'Enter Database Password (' + passwordDefault + '): '
    else:
      passwordPrompt = 'Enter Database Password: '
    passwordPattern = "^[a-zA-Z0-9_-]*$"
    passwordDescr = "Invalid characters in password. Use only alphanumeric or " \
                    "_ or - characters"

    password = read_password(passwordDefault, passwordPattern, passwordPrompt,
      passwordDescr)

    return password

  @staticmethod
  def _get_validated_db_name(database_storage_name, database_name):
    return get_validated_string_input(
        database_storage_name + " name ("
        + database_name + "): ",
        database_name,
        ".*",
        "Invalid " + database_storage_name.lower() + " name.",
        False
    )

  def _display_db_properties(self):
    print_info_msg('Using database options: {database},{host},{port},{schema},{user},{password}'.format(
        database=self.dbms,
        host=self.database_host,
        port=self.database_port,
        schema=self.database_name,
        user=self.database_username,
        password=self.database_password
    ))

  #Check if required jdbc drivers present
  @staticmethod
  def _find_jdbc_driver(jdbc_pattern):
    drivers = []
    drivers.extend(glob.glob(configDefaults.JAVA_SHARE_PATH + os.sep + jdbc_pattern))
    if drivers:
      return drivers
    return -1

  # Let the console user initialize the remote database schema
  def _setup_remote_db(self):
    setup_msg = "Before starting Ambari Server, you must run the following DDL " \
                "against the database to create the schema: {0}".format(self.init_script_file)

    print_warning_msg(setup_msg)

    proceed = get_YN_input("Proceed with configuring remote database connection properties [y/n] (y)? ", True)
    retCode = 0 if proceed else -1

    return retCode

  def _store_password_property(self, properties, property_name):
    properties.process_pair(property_name,
                            store_password_file(self.database_password, JDBC_PASSWORD_FILENAME))
    if self.isSecure:
      encrypted_password = encrypt_password(JDBC_RCA_PASSWORD_ALIAS, self.database_password)
      if encrypted_password != self.database_password:
        properties.process_pair(property_name, encrypted_password)

  # Store set of properties for remote database connection
  def _store_remote_properties(self, properties):
    properties.process_pair(PERSISTENCE_TYPE_PROPERTY, self.persistence_type)

    properties.process_pair(JDBC_DATABASE_PROPERTY, self.dbms)
    properties.process_pair(JDBC_HOSTNAME_PROPERTY, self.database_host)
    properties.process_pair(JDBC_PORT_PROPERTY, self.database_port)
    properties.process_pair(JDBC_DATABASE_NAME_PROPERTY, self.database_name)

    properties.process_pair(JDBC_DRIVER_PROPERTY, self.driver_class_name)
    # fully qualify the hostname to make sure all the other hosts can connect
    # to the jdbc hostname since its passed onto the agents for RCA
    jdbc_hostname = self.database_host
    if (self.database_host == "localhost"):
      jdbc_hostname = socket.getfqdn()

    connectionStringFormat = self.database_url_pattern
    if self.sid_or_sname == "sid":
      connectionStringFormat = self.database_url_pattern_alt
    properties.process_pair(JDBC_URL_PROPERTY, connectionStringFormat.format(jdbc_hostname, self.database_port, self.database_name))
    properties.process_pair(JDBC_USER_NAME_PROPERTY, self.database_username)

    self._store_password_property(properties, JDBC_PASSWORD_PROPERTY)

    # save any other defined properties to pass to JDBC
    for pair in self.jdbc_extra_params:
      properties.process_pair(JDBC_PROPERTIES_PREFIX + pair[0], pair[1])

    properties.process_pair(JDBC_RCA_DRIVER_PROPERTY, self.driver_class_name)
    properties.process_pair(JDBC_RCA_URL_PROPERTY, self.database_url_pattern.format(jdbc_hostname, self.database_port, self.database_name))
    properties.process_pair(JDBC_RCA_USER_NAME_PROPERTY, self.database_username)

    self._store_password_property(properties, JDBC_RCA_PASSWORD_FILE_PROPERTY)


# PostgreSQL configuration and setup
class PGConfig(LinuxDBMSConfig):
  # PostgreSQL settings
  SETUP_DB_CMD = ['su', '-', 'postgres',
                  '--command=psql -f {0} -v username=\'"{1}"\' -v password="\'{2}\'" -v dbname="{3}"']
  UPGRADE_STACK_CMD = ['su', 'postgres',
                       '--command=psql -f {0} -v stack_name="\'{1}\'"  -v stack_version="\'{2}\'" -v dbname="{3}"']

  CHANGE_OWNER_COMMAND = ['su', '-', 'postgres',
                          '--command=/var/lib/ambari-server/resources/scripts/change_owner.sh -d {0} -s {1} -o {2}']

  PG_ERROR_BLOCKED = "is being accessed by other users"
  PG_STATUS_RUNNING = None
  SERVICE_CMD = "/usr/bin/env service"
  PG_SERVICE_NAME = "postgresql"
  PG_HBA_DIR = None

  PG_ST_CMD = "%s %s status" % (SERVICE_CMD, PG_SERVICE_NAME)
  if os.path.isfile("/usr/bin/postgresql-setup"):
      PG_INITDB_CMD = "/usr/bin/postgresql-setup initdb"
  else:
      PG_INITDB_CMD = "%s %s initdb" % (SERVICE_CMD, PG_SERVICE_NAME)

  PG_START_CMD = "%s %s start" % (SERVICE_CMD, PG_SERVICE_NAME)
  PG_RESTART_CMD = "%s %s restart" % (SERVICE_CMD, PG_SERVICE_NAME)
  PG_HBA_RELOAD_CMD = "%s %s reload" % (SERVICE_CMD, PG_SERVICE_NAME)

  PG_HBA_CONF_FILE = None
  PG_HBA_CONF_FILE_BACKUP = None
  POSTGRESQL_CONF_FILE = None

  POSTGRES_EMBEDDED_INIT_FILE = "/var/lib/ambari-server/resources/Ambari-DDL-Postgres-EMBEDDED-CREATE.sql"
  POSTGRES_EMBEDDED_DROP_FILE = "/var/lib/ambari-server/resources/Ambari-DDL-Postgres-EMBEDDED-DROP.sql"

  POSTGRES_INIT_FILE = "/var/lib/ambari-server/resources/Ambari-DDL-Postgres-CREATE.sql"
  POSTGRES_DROP_FILE = "/var/lib/ambari-server/resources/Ambari-DDL-Postgres-DROP.sql"

  def __init__(self, options, properties, storage_type):
    super(PGConfig, self).__init__(options, properties, storage_type)

    #Init the database configuration data here, if any
    self.dbms = "postgres"
    self.dbms_full_name = "PostgreSQL"
    self.driver_class_name = "org.postgresql.Driver"
    self.driver_file_name = "postgresql-jdbc.jar"

    self.database_storage_name = "Database"

    # PostgreSQL seems to require additional schema coordinates
    self.postgres_schema = DBMSConfig._init_member_with_prop_default(options, "postgres_schema",
                                                                     properties, JDBC_POSTGRES_SCHEMA_PROPERTY, self.database_name)
    self.database_port = DBMSConfig._init_member_with_prop_default(options, "database_port",
                                                                   properties, JDBC_PORT_PROPERTY, "5432")

    self.database_url_pattern = "jdbc:postgresql://{0}:{1}/{2}"
    self.database_url_pattern_alt = "jdbc:postgresql://{0}:{1}/{2}"

    self.JDBC_DRIVER_INSTALL_MSG = 'Before starting Ambari Server, ' \
                                   'you must copy the {0} JDBC driver JAR file to {1}.'.format(
        self.dbms_full_name, configDefaults.JAVA_SHARE_PATH)

    self._is_user_changed = False

    if self.persistence_type == STORAGE_TYPE_LOCAL:
      PGConfig.PG_STATUS_RUNNING = get_postgre_running_status(OS_TYPE)
      PGConfig.PG_HBA_DIR = get_postgre_hba_dir(OS_FAMILY)

      PGConfig.PG_HBA_CONF_FILE = os.path.join(PGConfig.PG_HBA_DIR, "pg_hba.conf")
      PGConfig.PG_HBA_CONF_FILE_BACKUP = os.path.join(PGConfig.PG_HBA_DIR, "pg_hba_bak.conf.old")
      PGConfig.POSTGRESQL_CONF_FILE = os.path.join(PGConfig.PG_HBA_DIR, "postgresql.conf")

      postgres_init_file_default = PGConfig.POSTGRES_EMBEDDED_INIT_FILE
      postgres_drop_file_default = PGConfig.POSTGRES_EMBEDDED_DROP_FILE
    else:
      postgres_init_file_default = PGConfig.POSTGRES_INIT_FILE
      postgres_drop_file_default = PGConfig.POSTGRES_DROP_FILE
    self.init_script_file = DBMSConfig._init_member_with_default(options, "init_script_file",
                                                                 postgres_init_file_default)
    self.drop_tables_script_file = DBMSConfig._init_member_with_default(options, "drop_script_file",
                                                                        postgres_drop_file_default)
    self.client_tool_usage_pattern = 'su -postgres --command=psql -f {0} -v username=\'"{1}"\' -v password="\'{2}\'"'

  #
  # Public methods
  #
  def ensure_dbms_is_running(self, options, properties, scmStatus=None):
    if self._is_local_database():
      if is_root():
        (pg_status, retcode, out, err) = PGConfig._check_postgre_up()
        if not retcode == 0:
          err = 'Unable to start PostgreSQL server. Status {0}. {1}. Exiting'.format(pg_status, err)
          raise FatalException(retcode, err)
      else:
        print "Unable to check PostgreSQL server status when starting " \
              "without root privileges."
        print "Please do not forget to start PostgreSQL server."

  #
  # Private implementation
  #
  # Supporting remote server for all the DB types. Supporting local server only for PostgreSQL.
  def _setup_local_server(self, properties):
    # check if jdbc user is changed
    self._is_user_changed = PGConfig._is_jdbc_user_changed(self.database_username)
    print 'Default properties detected. Using built-in database.'
    self._store_local_properties(properties)

  def _setup_local_database(self):
    print 'Checking PostgreSQL...'
    (pg_status, retcode, out, err) = PGConfig._check_postgre_up()
    if not retcode == 0:
      err = 'Unable to start PostgreSQL server. Exiting'
      raise FatalException(retcode, err)
    print 'Configuring local database...'
    retcode, out, err = self._setup_db()
    if not retcode == 0:
      err = 'Running database init script failed. Exiting.'
      raise FatalException(retcode, err)
    if self._is_user_changed:
      #remove backup for pg_hba in order to reconfigure postgres
      remove_file(PGConfig.PG_HBA_CONF_FILE_BACKUP)
    print 'Configuring PostgreSQL...'
    retcode, out, err = self._configure_postgres()
    if not retcode == 0:
      err = 'Unable to configure PostgreSQL server. Exiting'
      raise FatalException(retcode, err)

  def _reset_local_database(self):
    #force reset if silent option provided
    if get_silent():
      default = "yes"
    else:
      default = "no"

    # Run automatic reset only for embedded DB
    okToRun = get_YN_input("Confirm server reset [yes/no]({0})? ".format(default), get_silent())
    if not okToRun:
      err = "Ambari Server 'reset' cancelled"
      raise FatalException(1, err)

    print "Resetting the Server database..."

    dbname = self.database_name
    filename = self.drop_tables_script_file
    username = self.database_username
    password = self.database_password
    command = PGConfig.SETUP_DB_CMD[:]
    command[-1] = command[-1].format(filename, username, password, dbname)
    drop_retcode, drop_outdata, drop_errdata = run_os_command(command)
    if not drop_retcode == 0:
      raise FatalException(1, drop_errdata)
    if drop_errdata and PGConfig.PG_ERROR_BLOCKED in drop_errdata:
      raise FatalException(1, "Database is in use. Please, make sure all connections to the database are closed")
    if drop_errdata and get_verbose():
      print_warning_msg(drop_errdata)
    print_info_msg("About to run database setup")
    retcode, outdata, errdata = self._setup_db()
    if errdata and get_verbose():
      print_warning_msg(errdata)
    if (errdata and 'ERROR' in errdata.upper()) or (drop_errdata and 'ERROR' in drop_errdata.upper()):
      err = "Non critical error in DDL"
      if not get_verbose():
        err += ", use --verbose for more information"
      raise NonFatalException(err)

  def _reset_remote_database(self):
    super(PGConfig, self)._reset_remote_database()

    raise NonFatalException("Please set DB password to PGPASSWORD env variable before running DDL`s!")

  def _is_jdbc_driver_installed(self, properties):
    return 0

  def _configure_database_name(self):
    self.database_name = LinuxDBMSConfig._get_validated_db_name(self.database_storage_name, self.database_name)
    self.postgres_schema = PGConfig._get_validated_db_schema(self.postgres_schema)
    return True

  def _get_remote_script_line(self, scriptFile):
    os.environ["PGPASSWORD"] = self.database_password
    return "psql -h {0} -p {1} -d {2} -U {3} -f {4} -v username='{3}'".format(
      self.database_host,
      self.database_port,
      self.database_name,
      self.database_username,
      scriptFile
    )

  @staticmethod
  def _get_validated_db_schema(postgres_schema):
    return get_validated_string_input(
        "Postgres schema (" + postgres_schema + "): ",
        postgres_schema,
        "^[a-zA-Z0-9_\-]*$",
        "Invalid schema name.",
        False, allowEmpty=True
    )

  # Check if jdbc user is changed
  @staticmethod
  def _is_jdbc_user_changed(database_username):
    properties = get_ambari_properties()
    if properties == -1:
      print_error_msg("Error getting ambari properties")
      return None

    previos_user = get_value_from_properties(properties, JDBC_USER_NAME_PROPERTY, "")

    if previos_user and database_username:
      if previos_user != database_username:
        return True
      else:
        return False

    return None

  # Store local database connection properties
  def _store_local_properties(self, properties):
    properties.removeOldProp(JDBC_DATABASE_PROPERTY)
    properties.removeOldProp(JDBC_DATABASE_NAME_PROPERTY)
    properties.removeOldProp(JDBC_POSTGRES_SCHEMA_PROPERTY)
    properties.removeOldProp(JDBC_HOSTNAME_PROPERTY)
    properties.removeOldProp(JDBC_RCA_DRIVER_PROPERTY)
    properties.removeOldProp(JDBC_RCA_URL_PROPERTY)
    properties.removeOldProp(JDBC_PORT_PROPERTY)
    properties.removeOldProp(JDBC_DRIVER_PROPERTY)
    properties.removeOldProp(JDBC_URL_PROPERTY)

    # Store the properties
    properties.process_pair(PERSISTENCE_TYPE_PROPERTY, self.persistence_type)
    properties.process_pair(JDBC_DATABASE_PROPERTY, self.dbms)
    properties.process_pair(JDBC_DATABASE_NAME_PROPERTY, self.database_name)
    properties.process_pair(JDBC_POSTGRES_SCHEMA_PROPERTY, self.postgres_schema)
    properties.process_pair(JDBC_USER_NAME_PROPERTY, self.database_username)

    self._store_password_property(properties, JDBC_PASSWORD_PROPERTY)

  @staticmethod
  def _get_postgre_status():
    retcode, out, err = run_os_command(PGConfig.PG_ST_CMD)
    try:
      pg_status = re.search('(stopped|running)', out, re.IGNORECASE).group(0).lower()
    except AttributeError:
      pg_status = None
    return pg_status, retcode, out, err

  @staticmethod
  def _check_postgre_up():
    pg_status, retcode, out, err = PGConfig._get_postgre_status()
    if pg_status == PGConfig.PG_STATUS_RUNNING:
      print_info_msg("PostgreSQL is running")
      return pg_status, 0, out, err
    else:
      # run initdb only on non ubuntu systems as ubuntu does not have initdb cmd.
      if OS_TYPE != OSConst.OS_UBUNTU:
        print "Running initdb: This may take upto a minute."
        retcode, out, err = run_os_command(PGConfig.PG_INITDB_CMD)
        if retcode == 0:
          print out
      print "About to start PostgreSQL"
      try:
        process = subprocess.Popen(PGConfig.PG_START_CMD.split(' '),
                                   stdout=subprocess.PIPE,
                                   stdin=subprocess.PIPE,
                                   stderr=subprocess.PIPE
        )
        if OS_TYPE == OSConst.OS_SUSE:
          time.sleep(20)
          result = process.poll()
          print_info_msg("Result of postgres start cmd: " + str(result))
          if result is None:
            process.kill()
            pg_status, retcode, out, err = PGConfig._get_postgre_status()
          else:
            retcode = result
        else:
          out, err = process.communicate()
          retcode = process.returncode
          pg_status, retcode, out, err = PGConfig._get_postgre_status()
        if pg_status == PGConfig.PG_STATUS_RUNNING:
          print_info_msg("Postgres process is running. Returning...")
          return pg_status, 0, out, err
      except (Exception), e:
        pg_status, retcode, out, err = PGConfig._get_postgre_status()
        if pg_status == PGConfig.PG_STATUS_RUNNING:
          return pg_status, 0, out, err
        else:
          print_error_msg("Postgres start failed. " + str(e))
      return pg_status, retcode, out, err

  def _setup_db(self):
    #password access to ambari-server and mapred
    dbname = self.database_name
    scriptFile = PGConfig.POSTGRES_EMBEDDED_INIT_FILE
    username = self.database_username
    password = self.database_password

    #setup DB
    command = PGConfig.SETUP_DB_CMD[:]
    command[-1] = command[-1].format(scriptFile, username, password, dbname)

    for i in range(SETUP_DB_CONNECT_ATTEMPTS):
      sys.stdout.write('Connecting to local database...')
      retcode, outdata, errdata = run_os_command(command)
      if retcode == 0:
        print 'done.'
        return retcode, outdata, errdata
      timeOutMsg = 'connection timed out'
      if (i+1) < SETUP_DB_CONNECT_ATTEMPTS:
        timeOutMsg += '...retrying (%d)' % (i+1)
        print timeOutMsg
        time.sleep(SETUP_DB_CONNECT_TIMEOUT)

    print 'unable to connect to database'
    print_error_msg(errdata)
    return retcode, outdata, errdata

  @staticmethod
  def _configure_pg_hba_ambaridb_users(conf_file, database_username):
    with open(conf_file, "a") as pgHbaConf:
      pgHbaConf.write("\n")
      pgHbaConf.write("local  all  " + database_username +
                      ",mapred md5")
      pgHbaConf.write("\n")
      pgHbaConf.write("host  all   " + database_username +
                      ",mapred 0.0.0.0/0  md5")
      pgHbaConf.write("\n")
      pgHbaConf.write("host  all   " + database_username +
                      ",mapred ::/0 md5")
      pgHbaConf.write("\n")
    retcode, out, err = run_os_command(PGConfig.PG_HBA_RELOAD_CMD)
    if not retcode == 0:
      raise FatalException(retcode, err)

  @staticmethod
  def _configure_pg_hba_postgres_user():
    postgresString = "all   postgres"
    for line in fileinput.input(PGConfig.PG_HBA_CONF_FILE, inplace=1):
      print re.sub('all\s*all', postgresString, line),
    os.chmod(PGConfig.PG_HBA_CONF_FILE, 0644)

  @staticmethod
  def _configure_postgresql_conf():
    listenAddress = "listen_addresses = '*'        #"
    for line in fileinput.input(PGConfig.POSTGRESQL_CONF_FILE, inplace=1):
      print re.sub('#+listen_addresses.*?(#|$)', listenAddress, line),
    os.chmod(PGConfig.POSTGRESQL_CONF_FILE, 0644)

  def _configure_postgres(self):
    if os.path.isfile(PGConfig.PG_HBA_CONF_FILE):
      if not os.path.isfile(PGConfig.PG_HBA_CONF_FILE_BACKUP):
        shutil.copyfile(PGConfig.PG_HBA_CONF_FILE, PGConfig.PG_HBA_CONF_FILE_BACKUP)
      else:
        #Postgres has been configured before, must not override backup
        print "Backup for pg_hba found, reconfiguration not required"
        return 0, "", ""
    PGConfig._configure_pg_hba_postgres_user()
    PGConfig._configure_pg_hba_ambaridb_users(PGConfig.PG_HBA_CONF_FILE, self.database_username)
    os.chmod(PGConfig.PG_HBA_CONF_FILE, 0644)
    PGConfig._configure_postgresql_conf()
    #restart postgresql if already running
    pg_status, retcode, out, err = PGConfig._get_postgre_status()
    if pg_status == PGConfig.PG_STATUS_RUNNING:
      retcode, out, err = PGConfig._restart_postgres()
      return retcode, out, err
    return 0, "", ""

  @staticmethod
  def _restart_postgres():
    print "Restarting PostgreSQL"
    process = subprocess.Popen(PGConfig.PG_RESTART_CMD.split(' '),
                               stdout=subprocess.PIPE,
                               stdin=subprocess.PIPE,
                               stderr=subprocess.PIPE
    )
    time.sleep(5)
    result = process.poll()
    if result is None:
      print_info_msg("Killing restart PostgresSQL process")
      process.kill()
      pg_status, retcode, out, err = PGConfig._get_postgre_status()
      # SUSE linux set status of stopped postgresql proc to unused
      if pg_status == "unused" or pg_status == "stopped":
        print_info_msg("PostgreSQL is stopped. Restarting ...")
        retcode, out, err = run_os_command(PGConfig.PG_START_CMD)
        return retcode, out, err
    return 0, "", ""

  def _store_remote_properties(self, properties):
    super(PGConfig, self)._store_remote_properties(properties)

    properties.process_pair(JDBC_POSTGRES_SCHEMA_PROPERTY, self.postgres_schema)

  def _change_db_files_owner(self):
    database_name = self.database_name
    new_owner = self.database_username
    if '"' not in new_owner:
      #wrap to allow old username "ambari-server", postgres only
      new_owner = '\'"{0}"\''.format(new_owner)
      pass

    command = PGConfig.CHANGE_OWNER_COMMAND[:]
    command[-1] = command[-1].format(database_name, 'ambari', new_owner)
    retcode, stdout, stderr = run_os_command(command)
    if not retcode == 0:
      if get_verbose():
        if stderr:
          print_error_msg("stderr:\n" + stderr.strip())
        if stdout:
          print_error_msg("stdout:\n" + stdout.strip())
    else:
      print_info_msg('Fixed database objects owner')

    return retcode

def createPGConfig(options, properties, storage_type, dbId):
    return PGConfig(options, properties, storage_type)

class OracleConfig(LinuxDBMSConfig):
  def __init__(self, options, properties, storage_type):
    super(OracleConfig, self).__init__(options, properties, storage_type)

    #Init the database configuration data here, if any
    self.dbms = "oracle"
    self.dbms_full_name = "Oracle"
    self.driver_class_name = "oracle.jdbc.driver.OracleDriver"
    self.driver_file_name = "ojdbc6.jar"
    self.driver_symlink_name = "oracle-jdbc-driver.jar"

    self.database_storage_name = "Service"

    if (hasattr(options, 'sid_or_sname') and options.sid_or_sname == "sname") or \
        (hasattr(options, 'jdbc_url') and options.jdbc_url and re.match(ORACLE_SNAME_PATTERN, options.jdbc_url)):
      print_info_msg("using SERVICE_NAME instead of SID for Oracle")
      self.sid_or_sname = "service_name"

    self.database_port = DBMSConfig._init_member_with_prop_default(options, "database_port",
                                                                   properties, JDBC_PORT_PROPERTY, "1521")

    self.database_url_pattern = "jdbc:oracle:thin:@{0}:{1}/{2}"
    self.database_url_pattern_alt = "jdbc:oracle:thin:@{0}:{1}:{2}"

    self.JDBC_DRIVER_INSTALL_MSG = 'Before starting Ambari Server, ' \
                                   'you must copy the {0} JDBC driver JAR file to {1}.'.format(
        self.dbms_full_name, configDefaults.JAVA_SHARE_PATH)

    self.init_script_file = "/var/lib/ambari-server/resources/Ambari-DDL-Oracle-CREATE.sql'"
    self.drop_tables_script_file = "/var/lib/ambari-server/resources/Ambari-DDL-Oracle-DROP.sql"
    self.client_tool_usage_pattern = 'sqlplus {1}/{2} < {0}'

    self.jdbc_extra_params = [
        ["oracle.net.CONNECT_TIMEOUT", "2000"], # socket level timeout
        ["oracle.net.READ_TIMEOUT", "2000"], # socket level timeout
        ["oracle.jdbc.ReadTimeout", "8000"] # query fetch timeout
    ]

  #
  # Private implementation
  #
  def _reset_remote_database(self):
    super(OracleConfig, self)._reset_remote_database()

    raise NonFatalException("Please replace '*' symbols with password before running DDL`s!")

  def _is_jdbc_driver_installed(self, properties):
    return LinuxDBMSConfig._find_jdbc_driver("*ojdbc*.jar")

  def _configure_database_name(self):
    if self.persistence_type != STORAGE_TYPE_LOCAL:
      # Oracle uses service name or service id
      idType = "1"
      idType = get_validated_string_input(
          "Select Oracle identifier type:\n1 - " + ORACLE_DB_ID_TYPES[0] +
          "\n2 - " + ORACLE_DB_ID_TYPES[1] + "\n(" + idType + "): ",
          idType,
          "^[12]$",
          "Invalid number.",
          False
      )

      if idType == "2":
        self.sid_or_sname = "sid"

      IDTYPE_INDEX = int(idType) - 1
      self.database_name = OracleConfig._get_validated_service_name(self.database_name,
                                                                    IDTYPE_INDEX)
    else:
      self.database_name = LinuxDBMSConfig._get_validated_db_name(self.database_storage_name, self.database_name)

    return True

  def _get_remote_script_line(self, scriptFile):
    # Detect the existing sqlplus flavor
    try:
      find_in_path("sqlplus64")
      tool = "sqlplus64"
    except:
      tool = "sqlplus"

    ORACLE_EXEC_ARGS = "{0} -S -L '{1}/{2}@(description=(address=(protocol=TCP)(host={3})(port={4}))(connect_data=({7}={5})))' @{6} {1}"

    return ORACLE_EXEC_ARGS.format(
      tool,
      self.database_username,
      self.database_password,
      self.database_host,
      self.database_port,
      self.database_name,
      scriptFile,
      self.sid_or_sname
    )

  @staticmethod
  def _get_validated_service_name(service_name, index):
    return get_validated_string_input(
        ORACLE_DB_ID_TYPES[index] + " (" + service_name + "): ",
        service_name,
        ".*",
        "Invalid " + ORACLE_DB_ID_TYPES[index] + ".",
        False
    )

def createOracleConfig(options, properties, storage_type, dbId):
  return OracleConfig(options, properties, storage_type)


class MySQLConfig(LinuxDBMSConfig):
  def __init__(self, options, properties, storage_type):
    super(MySQLConfig, self).__init__(options, properties, storage_type)

    #Init the database configuration data here, if any
    self.dbms = "mysql"
    self.dbms_full_name = "MySQL"
    self.driver_class_name = "com.mysql.jdbc.Driver"
    self.driver_file_name = "mysql-connector-java.jar"
    self.driver_symlink_name = "mysql-jdbc-driver.jar"

    self.database_storage_name = "Database"
    self.database_port = DBMSConfig._init_member_with_prop_default(options, "database_port",
                                                                   properties, JDBC_PORT_PROPERTY, "3306")

    self.database_url_pattern = "jdbc:mysql://{0}:{1}/{2}"
    self.database_url_pattern_alt = "jdbc:mysql://{0}:{1}/{2}"

    self.JDBC_DRIVER_INSTALL_MSG = 'Before starting Ambari Server, ' \
                                     'you must copy the {0} JDBC driver JAR file to {1}.'.format(
    self.dbms_full_name, configDefaults.JAVA_SHARE_PATH)

    self.init_script_file = "/var/lib/ambari-server/resources/Ambari-DDL-MySQL-CREATE.sql"
    self.drop_tables_script_file = "/var/lib/ambari-server/resources/Ambari-DDL-MySQL-DROP.sql"
    self.client_tool_usage_pattern = 'mysql --user={1} --password={2} {3}<{0}'

  #
  # Private implementation
  #
  def _reset_remote_database(self):
    super(MySQLConfig, self)._reset_remote_database()

    raise NonFatalException("Please replace '*' symbols with password before running DDL`s!")

  def _is_jdbc_driver_installed(self, properties):
    return LinuxDBMSConfig._find_jdbc_driver("*mysql*.jar")

  def _configure_database_name(self):
    self.database_name = LinuxDBMSConfig._get_validated_db_name(self.database_storage_name, self.database_name)
    return True

  def _get_remote_script_line(self, scriptFile):
    MYSQL_INIT_SCRIPT = '/var/lib/ambari-server/resources/Ambari-DDL-MySQL-CREATE.sql'
    MYSQL_EXEC_ARGS_WITH_USER_VARS = "mysql --host={0} --port={1} --user={2} --password={3} {4} " \
                                     "-e\"set @schema=\'{4}\'; set @username=\'{2}\'; source {5};\""
    MYSQL_EXEC_ARGS_WO_USER_VARS = "mysql --force --host={0} --port={1} --user={2} --password={3} --database={4} < {5} 2> /dev/null"

    MYSQL_EXEC_ARGS = MYSQL_EXEC_ARGS_WO_USER_VARS if MYSQL_INIT_SCRIPT == scriptFile else MYSQL_EXEC_ARGS_WITH_USER_VARS
    return MYSQL_EXEC_ARGS.format(
      self.database_host,
      self.database_port,
      self.database_username,
      self.database_password,
      self.database_name,
      scriptFile
    )

def createMySQLConfig(options, properties, storage_type, dbId):
  return MySQLConfig(options, properties, storage_type)
