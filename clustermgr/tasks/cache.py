import json
import os
import re

from StringIO import StringIO

from clustermgr.models import Server, AppConfiguration
from clustermgr.extensions import db, celery, wlogger
from clustermgr.core.remote import RemoteClient
from clustermgr.core.ldap_functions import DBManager

from ldap3.core.exceptions import LDAPSocketOpenError
from flask import current_app as app


class BaseInstaller(object):
    """Base class for component installers.

    Args:
        server (class:`clustermgr.models.Server`): the server object denoting
            the server where server should be installed
        tid (string): the task id of the celery task to add logs
    """

    def __init__(self, server, tid):
        self.server = server
        self.tid = tid
        self.rc = RemoteClient(server.hostname, ip=server.ip)
        self.chdir = None
        if self.server.gluu_server:
            version = AppConfiguration.query.first().gluu_version
            self.chdir = "/opt/gluu-server-{0}".format(version)

    def install(self):
        """install() detects the os of the server and calls the appropriate
        function to install redis on that server.

        Returns:
            boolean status of the installs
        """
        try:
            self.rc.startup()
        except Exception as e:
            wlogger.log(self.tid, "Could not connect to {0}".format(e),
                        "error", server_id=self.server.id)
            return False

        cin, cout, cerr = self.rc.run("ls /etc/*release")
        files = cout.split()
        cin, cout, cerr = self.rc.run("cat " + files[0])

        if "Ubuntu" in cout:
            return self.install_in_ubuntu()
        if "CentOS" in cout:
            return self.install_in_centos()
        else:
            wlogger.log(self.tid, "Server OS is not supported. {0}".format(
                cout), "error", server_id=self.server.id)
            return False

    def install_in_ubuntu(self):
        """This method should be overridden by the sub classes. Run the
        commands needed to install your component.

        Returns:
            boolean status of success of the install
        """
        pass

    def install_in_centos(self):
        """This method should be overridden by the sub classes. Run the
        commands needed to install your component.

        Returns:
            boolean status of success of the install
        """
        pass

    def _chcmd(self, cmd):
        if self.chdir:
            return 'chroot {0} /bin/bash -c "{1}"'.format(self.chdir, cmd)
        else:
            return cmd

    def run_command(self, cmd):
        wlogger.log(self.tid, self._chcmd(cmd), "debug",
                    server_id=self.server.id)
        return self.rc.run(self._chcmd(cmd))


class RedisInstaller(BaseInstaller):
    """RedisInstaller installs redis-server in the provided server. Refer to
    `BaseInstaller` for docs.
    """

    def install_in_ubuntu(self):
        self.run_command("apt-get update")
        self.run_command("apt-get upgrade -y")
        self.run_command("apt-get install software-properties-common -y")
        self.run_command("add-apt-repository ppa:chris-lea/redis-server -y")
        self.run_command("apt-get update")
        cin, cout, cerr = self.run_command("apt-get install redis-server -y")
        wlogger.log(self.tid, cout, "debug", server_id=self.server.id)
        if cerr:
            wlogger.log(self.tid, cerr, "cerror", server_id=self.server.id)
        # verifying that redis-server is succesfully installed
        cin, cout, cerr = self.rc.run(
            self._chcmd("apt-get install redis-server -y"))

        if "redis-server is already the newest version" in cout:
            return True
        else:
            return False

    def install_in_centos(self):
        # To automatically start redis on boot
        # systemctl enable redis
        self.run_command("yum update -y")
        self.run_command("yum install epel-release -y")
        self.run_command("yum update -y")

        cin, cout, cerr = self.run_command("yum install redis -y")
        wlogger.log(self.tid, cout, "debug", server_id=self.server.id)
        if cerr:
            wlogger.log(self.tid, cerr, "cerror", server_id=self.server.id)
        # TODO find the successful install message and return True
        if cerr:
            return False
        return True


class StunnelInstaller(BaseInstaller):
    def install_in_ubuntu(self):
        self.run_command("apt-get update")
        cin, cout, cerr = self.run_command("apt-get install stunnel4 -y")
        wlogger.log(self.tid, cout, "debug", server_id=self.server.id)
        if cerr:
            wlogger.log(self.tid, cerr, "cerror", server_id=self.server.id)

        # Verifying installation by trying to reinstall
        cin, cout, cerr = self.rc.run(
            self._chcmd("apt-get install stunnel4 -y"))
        if "stunnel4 is already the newest version" in cout:
            return True
        else:
            return False

    def install_in_centos(self):
        self.run_command("yum update -y")
        cin, cout, cerr = self.run_command("yum install stunnel -y")
        wlogger.log(self.tid, cout, "debug", server_id=self.server.id)
        if cerr:
            wlogger.log(self.tid, cerr, "cerror", server_id=self.server.id)
        # TODO find the successful install message and return True
        if cerr:
            return False
        return True


def setup_sharded(tid):
    servers = Server.query.filter(Server.redis.is_(True)).filter(
        Server.stunnel.is_(True)).all()
    appconf = AppConfiguration.query.first()
    # Store the redis server info in the LDAP
    ports_count = len(servers) - 1
    for server in servers:
        wlogger.log(tid, "Updating oxCacheConfiguration ...", "debug",
                    server_id=server.id)

        server_string = ",".join(['localhost:6379'] +
                                 ["localhost:700{0}".format(i) for i in
                                  xrange(ports_count)])

        try:
            dbm = DBManager(server.hostname, 1636, server.ldap_password,
                            ssl=True, ip=server.ip, )
        except Exception as e:
            wlogger.log(tid, "Couldn't connect to LDAP. Error: {0}".format(e),
                        "error", server_id=server.id)
            wlogger.log(tid, "Make sure your LDAP server is listening to "
                             "connections from outside", "debug",
                        server_id=server.id)
            continue
        entry = dbm.get_appliance_attributes('oxCacheConfiguration')
        cache_conf = json.loads(entry.oxCacheConfiguration.value)
        cache_conf['cacheProviderType'] = 'REDIS'
        cache_conf['redisConfiguration']['redisProviderType'] = 'SHARDED'
        cache_conf['redisConfiguration']['servers'] = server_string

        result = dbm.set_applicance_attribute('oxCacheConfiguration',
                                              [json.dumps(cache_conf)])
        if not result:
            wlogger.log(tid, "oxCacheConfigutaion update failed", "fail",
                        server_id=server.id)
            continue

        # generate stunnel configuration and upload it to the server
        wlogger.log(tid, "Setting up stunnel", "info", server_id=server.id)
        chdir = '/'
        if server.gluu_server:
            chdir = "/opt/gluu-server-{0}".format(appconf.gluu_version)

        rc = RemoteClient(server.hostname, ip=server.ip)
        try:
            rc.startup()
        except:
            wlogger.log(tid, "Could not connect to the server over SSH. "
                        "Stunnel setup failed.", "error", server_id=server.id)
            continue

        wlogger.log(tid, "Enable stunnel start on system boot", "debug",
                    server_id=server.id)
        # replace the /etc/default/stunnel4 to enable start on system startup
        local = os.path.join(app.root_path, 'templates', 'stunnel',
                             'stunnel4.default')
        remote = os.path.join(chdir, 'etc/default/stunnel4')
        rc.upload(local, remote)

        # setup the certificate file
        wlogger.log(tid, "Generating certificate for stunnel ...", "debug",
                    server_id=server.id)
        prop_buffer = StringIO()
        propsfile = os.path.join(chdir, "install", "community-edition-setup",
                                 "setup.properties.last")
        rc.sftpclient.getfo(propsfile, prop_buffer)
        prop_buffer.seek(0)
        props = dict()
        prop_in = lambda line: line.split("=")[1].strip()
        for line in prop_buffer:
            if re.match('^countryCode', line):
                props['country'] = prop_in(line)
            if re.match('^state', line):
                props['state'] = prop_in(line)
            if re.match('^city', line):
                props['city'] = prop_in(line)
            if re.match('^orgName', line):
                props['org'] = prop_in(line)
            if re.match('^hostname', line):
                props['cn'] = prop_in(line)
            if re.match('^admin_email', line):
                props['email'] = prop_in(line)

        subject = "'/C={country}/ST={state}/L={city}/O={org}/CN={cn}" \
                  "/emailAddress={email}'".format(**props)
        cert_path = os.path.join(chdir, "etc", "stunnel", "server.crt")
        key_path = os.path.join(chdir, "etc", "stunnel", "server.key")
        pem_path = os.path.join(chdir, "etc", "stunnel", "cert.pem")
        cmd = ["/usr/bin/openssl", "req", "-subj", subject, "-new", "-newkey",
               "rsa:2048", "-sha256", "-days", "365", "-nodes", "-x509",
               "-keyout", key_path, "-out", cert_path]
        cin, cout, cerr = rc.run(" ".join(cmd))
        rc.run("cat {cert} {key} > {pem}".format(cert=cert_path, key=key_path,
                                                 pem=pem_path))
        # verify certificate
        cin, cout, cerr = rc.run("/usr/bin/openssl verify " + pem_path)
        if props['cn'] in cout and props['org'] in cout:
            wlogger.log(tid, "Certificate generated successfully", "success",
                        server_id=server.id)
        else:
            wlogger.log(tid, "Certificate generation failed. Add a SSL "
                             "certificate at /etc/stunnel/cert.pem", "error",
                        server_id=server.id)

        # Generate stunnel config
        wlogger.log(tid, "Setup stunnel listening and forwarding", "debug",
                    server_id=server.id)
        sconf = ["cert = /etc/stunnel/cert.pem",
                 "pid = /var/run/stunnel.pid",
                 "[redis-server]",
                 "client = no",
                 "accept = {0}:7777".format(server.ip),
                 "connect = 127.0.0.1:6379"
                 ]
        listen_port = 7000
        for s in servers:
            if s.id != server.id:
                sconf.append("[client{0}]".format(s.id))
                sconf.append("client = yes")
                sconf.append("accept = 127.0.0.1:{0}".format(listen_port))
                sconf.append("connect = {0}:7777".format(s.ip))
                listen_port += 1

        rc.put_file(os.path.join(chdir, "etc/stunnel/redis-gluu.conf"),
                    "\n".join(sconf))
    return True


def setup_redis_cluster(tid):
    servers = Server.query.filter(Server.redis.is_(True)).filter(
        Server.stunnel.is_(True)).all()
    appconf = AppConfiguration.query.first()

    master_conf = ["port 7000", "cluster-enabled yes",
                   "cluster-config-file nodes_7000.conf",
                   "cluster-node-timeout 5000",
                   "appendonly yes"]
    slave_conf = ["port 7001", "cluster-enabled yes",
                  "cluster-config-file nodes_7001.conf",
                  "cluster-node-timeout 5000",
                  "appendonly yes"]
    for server in servers:
        rc = RemoteClient(server.hostname, ip=server.ip)
        try:
            rc.startup()
            wlogger.log(tid, "Connecting to server ...", "success",
                        server_id=server.id)
        except Exception as e:
            wlogger.log(tid, "Could not connect to the server over SSH. Error:"
                        "{0}\nRedis configuration failed.".format(e), "error",
                        server_id=server.id)
            continue

        chdir = '/'
        if server.gluu_server:
            chdir = "/opt/gluu-server-{0}".format(appconf.gluu_version)
        # upload the conf files
        wlogger.log(tid, "Uploading redis conf files...", "debug",
                    server_id=server.id)
        rc.put_file(os.path.join(chdir, "etc/redis/redis_7000.conf"),
                    "\n".join(master_conf))
        rc.put_file(os.path.join(chdir, "etc/redis/redis_7001.conf"),
                    "\n".join(slave_conf))
        # upload the modified init.d file
        rc.upload(os.path.join(
            app.root_path, "templates", "redis", "redis-server"),
            os.path.join(chdir, "etc/init.d/redis-server"))
        wlogger.log(tid, "Configuration upload complete.", "success",
                    server_id=server.id)

    return True


def startup_redis_cluster(tid):
    pass


@celery.task(bind=True)
def install_redis_stunnel(self, servers):
    """Celery task that installs the redis and stunnel software in the given
    list of servers.

    :param servers: list of server ids (int)
    :return: the number of servers where both stunnel and redis were installed
        successfully
    """
    tid = self.request.id
    installed = 0
    for server_id in servers:
        server = Server.query.get(server_id)
        wlogger.log(tid, "Installing Redis in server {0}".format(
            server.hostname), "info", server_id=server_id)
        ri = RedisInstaller(server, tid)
        redis_installed = ri.install()
        if redis_installed:
            server.redis = True
            wlogger.log(tid, "Redis install successful", "success",
                        server_id=server_id)
        else:
            server.redis = False
            wlogger.log(tid, "Redis install failed", "fail",
                        server_id=server_id)

        wlogger.log(tid, "Installing Stunnel", "info", server_id=server_id)
        si = StunnelInstaller(server, tid)
        stunnel_installed = si.install()
        if stunnel_installed:
            server.stunnel = True
            wlogger.log(tid, "Stunnel install successful", "success",
                        server_id=server_id)
        else:
            server.stunnel = False
            wlogger.log(tid, "Stunnel install failed", "fail",
                        server_id=server_id)
        # Save the redis and stunnel install situation to the db
        db.session.commit()

        if redis_installed and stunnel_installed:
            installed += 1
    return installed


@celery.task(bind=True)
def configure_cache_cluster(self, method):
    if method == 'SHARDED':
        return setup_sharded(self.request.id)
    elif method == 'CLUSTER':
        return setup_redis_cluster(self.request.id)


@celery.task(bind=True)
def finish_cluster_setup(self, method):
    tid = self.request.id
    # TODO steps to finish the clustering
    # if method is cluster
    #       start multiple instances of the cluster server and initiate cluster
    # if method is sharding
    #       start redis server in default port 6379
    # start stunnel
    # restart oxauth service
    # restart identity service
    if method == 'CLUSTER':
        startup_redis_cluster(self.request.id)


@celery.task(bind=True)
def get_cache_methods(self):
    tid = self.request.id
    servers = Server.query.all()
    methods = []
    for server in servers:
        try:
            dbm = DBManager(server.hostname, 1636, server.ldap_password,
                            ssl=True, ip=server.ip)
        except LDAPSocketOpenError as e:
            wlogger.log(tid, "Couldn't connect to server {0}. Error: "
                             "{1}".format(server.hostname, e), "error")
            continue

        entry = dbm.get_appliance_attributes('oxCacheConfiguration')
        cache_conf = json.loads(entry.oxCacheConfiguration.value)
        server.cache_method = cache_conf['cacheProviderType']
        db.session.commit()
        methods.append({"id": server.id, "method": server.cache_method})
    wlogger.log(tid, "Cache Methods of servers have been updated.", "success")
    return methods
