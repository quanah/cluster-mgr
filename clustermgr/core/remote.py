import StringIO
import socket

from paramiko import SSHException
from paramiko.client import SSHClient, AutoAddPolicy


class ClientNotSetupException(Exception):
    """Exception raised when the client is not initialized because
    of connection failures."""
    pass


class RemoteClient(object):
    """Remote Client is a wrapper over SSHClient with utility functions.

    Args:
        host (string): The hostname of the server to connect. It can be an IP
            address of the server also.
        user (string, optional): The user to connect to the remote server. It
            defaults to root

    Attributes:
        host (string): The hostname passed in as a the argument
        user (string): The user to connect as to the remote server
        client (:class:`paramiko.client.SSHClient`): The SSHClient object used
            for all the communications with the remote server.
        sftpclient (:class:`paramiko.sftp_client.SFTPClient`): The SFTP object
            for all the file transfer operations over the SSH.
    """

    def __init__(self, host, ip=None, user='root'):
        self.host = host
        self.ip = ip
        self.user = user
        self.client = SSHClient()
        self.sftpclient = None
        self.client.set_missing_host_key_policy(AutoAddPolicy())
        self.client.load_system_host_keys()

    def startup(self):
        """Function that starts SSH connection and makes client available for
        carrying out the functions. It tries with the hostname, if it fails
        it tries with the IP address if supplied
        """
        try:
            self.client.connect(self.host, port=22, username=self.user)
            self.sftpclient = self.client.open_sftp()
        except (SSHException, socket.error):
            self._try_with_ip()

    def _try_with_ip(self):
        try:
            self.client.connect(self.ip, port=22, username=self.user)
            self.sftpclient = self.client.open_sftp()
        except (SSHException, socket.error):
            raise ClientNotSetupException('Could not connect to the host.')

    def download(self, remote, local):
        """Downloads a file from remote server to the local system.

        Args:
            remote (string): location of the file in remote server
            local (string): path where the file should be saved
        """
        if not self.sftpclient:
            raise ClientNotSetupException(
                'Cannot download file. Client not initialized')

        try:
            self.sftpclient.get(remote, local)
            return "Download successful. File at: {0}".format(local)
        except OSError:
            return "Error: Local file %s doesn't exist." % local
        except IOError:
            return "Error: Remote location %s doesn't exist." % remote

    def upload(self, local, remote):
        """Uploads the file from local location to remote server.

        Args:
            local (string): path of the local file to upload
            remote (string): location on remote server to put the file
        """
        if not self.sftpclient:
            raise ClientNotSetupException(
                'Cannot upload file. Client not initialized')

        try:
            self.sftpclient.put(local, remote)
            return "Upload successful. File at: {0}".format(remote)
        except OSError:
            return "Error: Local file %s doesn't exist." % local
        except IOError:
            return "Error: Remote location %s doesn't exist." % remote

    def exists(self, filepath):
        """Returns whether a file exists or not in the remote server.

        Args:
            filepath (string): path to the file to check for existance

        Returns:
            True if it exists, False if it doesn't
        """
        if not self.client:
            raise ClientNotSetupException(
                'Cannot run procedure. Client not initialized')
        cin, cout, cerr = self.client.exec_command('stat {0}'.format(filepath))
        if len(cout.read()) > 5:
            return True
        elif len(cerr.read()) > 5:
            return False

    def run(self, command):
        """Run a command in the remote server.

        Args:
            command (string): the command to be run on the remote server

        Returns:
            tuple of three strings containing text from stdin, stdout an stderr
        """
        if not self.client:
            raise ClientNotSetupException(
                'Cannot run procedure. Client not initialized')

        buffers = self.client.exec_command(command, timeout=30)
        output = []
        for buf in buffers:
            try:
                output.append(buf.read())
            except IOError:
                output.append('')

        return tuple(output)

    def get_file(self, filename):
        """Reads content of filename on remote server

        Args:
            filename (string): name of file to be read from remote server

        Returns:
            tuple: True/False, file like object / error
        """
        f = StringIO.StringIO()
        try:
            r = self.sftpclient.getfo(filename, f)
            f.seek(0)
            return r, f
        except Exception as err:
            return False, err
    
    def put_file(self,  filename, filecontent):
        """Puts content to a file on remote server

        Args:
            filename (string): name of file to be written on remote server
            filecontent (string): content of file

        Returns:
            tuple: True/False, file size / error
        """
        f = StringIO.StringIO()
        f.write(filecontent)
        f.seek(0)

        try:
            r = self.sftpclient.putfo(f, filename)
            return True, r.st_size
        except Exception as err:
            return False, err

    def mkdir(self,  dirname):
        """Creates a new directory.

        Args:
            dirname (string): the full path of the directory that needs to be
                created

        Returns:
            a tuple containing the success or failure of operation and dirname
                on success and error on failure
        """
        try:
            self.sftpclient.mkdir(dirname)
            return True, dirname
        except Exception as err:
            return False, err

    def listdir(self, dirname):
        """Lists all the files and folders in a directory.

        Args:
            dirname (string): the full path of the directory that needs to be
                listed

        Returns:
            a list of the files and folders in the directory
        """
        try:
            r = self.sftpclient.listdir(dirname)
            return True, r
        except Exception as err:
            return False, err

    def close(self):
        """Close the SSH Connection
        """
        self.client.close()
