"""
aws
^^^

Simple module for writing generating and writing out AWS Credentials into your
~/.aws/credentials file with a supplied Saml assertion.

Credits: This code base was almost entirely stolen from
https://github.com/ThoughtWorksInc/aws_role_credentials. It continues to be
modified from the original code, but thanks a ton to the original writers at
Thought Works Inc.
"""

import boto3
import configparser
import datetime
import logging
import os
import xml
import json

from os.path import expanduser

from aws_role_credentials import models

log = logging.getLogger(__name__)


class BaseException(Exception):
    """Base AWS SAML Exception"""


class InvalidSaml(BaseException):
    """Raised when the SAML Assertion is invalid for some reason"""


class Credentials(object):
    """Simple AWS Credentials Profile representation.

    This object reads in an Amazon ~/.aws/credentials file, and then allows you
    to write out credentials into different Profile sections.
    """

    def __init__(self, filename):
        self.filename = filename

    def _add_profile(self, name, profile):
        config = configparser.ConfigParser(interpolation=None)
        try:
            config.read_file(open(self.filename, 'r'))
        except IOError:
            pass

        if not config.has_section(name):
            config.add_section(name)

        [(config.set(name, k, v)) for k, v in profile.items()]
        with open(self.filename, 'w+') as configfile:
            config.write(configfile)

    def add_profile(self, name, region, access_key, secret_key, session_token):
        """Writes out a set of AWS Credentials to disk.

        args:
            name: The profile name to write to
            region: The region to use as the default region for this profile
            access_key: The AWS_ACCESS_KEY_ID
            secret_key: The AWS_SECRET_ACCESS_KEY
            session_token: The AWS_SESSION_TOKEN
        """
        name = unicode(name)
        self._add_profile(
            name,
            {u'output': u'json',
             u'region': unicode(region),
             u'aws_access_key_id': unicode(access_key),
             u'aws_secret_access_key': unicode(secret_key),
             u'aws_security_token': unicode(session_token),
             u'aws_session_token': unicode(session_token)
             })
        if name == "default":
            log.info('Updated default profile'.format(
                name=name, file=self.filename))
        else:
            log.info('Wrote profile "{name}" to {file}'.format(
                name=name, file=self.filename))


class ExpirationRecord(object):
    """Simple on-disk file to record session expiration time
    """

    def __init__(self, filename):
        self.filename = filename

    def _write_expiration(self, expiration_time):
        try:
            with open(self.filename, 'r') as outfile:
                json.dump(expiration_time, outfile)
        except IOError:
            pass

    def write_expiration(self, name):
        """Writes expiration out to disk
        """
        name = unicode(name)
        self._write_expiration(
            name,
            {u'region': unicode(region),
             u'output': u'json',
             u'expiration_time': unicode(expiration_time)
             })
        log.info("Wrote expiration time to to {file}".format(
            name=name, file=self.filename))



class Session(object):
    """Amazon Federated Session Generator.

    This class is used to contact Amazon with a specific SAML Assertion and
    get back a set of temporary Federated credentials. These credentials are
    written to disk (using the Credentials object above).

    This object is meant to be used once -- as SAML Assertions are one-time-use
    objects.
    """

    def __init__(self,
                 assertion,
                 credential_path='~/.aws',
                 profile='default',
                 region='us-east-1'):
        cred_dir = expanduser(credential_path)
        cred_file = os.path.join(cred_dir, 'credentials')

        boto_logger = logging.getLogger('botocore')
        boto_logger.setLevel(logging.WARNING)

        if not os.path.exists(cred_dir):
            log.info('Creating AWS credential dir {dir}'.format(
                dir=cred_dir))
            os.makedirs(cred_dir)

        self.sts = boto3.client('sts')

        self.profile = profile
        self.region = region

        self.assertion = models.SamlAssertion(assertion)
        self.writer = Credentials(cred_file)

        # Populated by self.assume_role()
        self.aws_access_key_id = None
        self.aws_secret_access_key = None
        self.aws_session_token = None
        self.expiration = None

    @property
    def is_within_renewal_buffer(self):
        """Returns True if the Session is still valid.

        Takes the current time (in UTC) and compares it to the Expiration time
        returned by Amazon. Adds a 10 minute buffer to make sure that we start
        working to renew the creds far before they really expire and break.

        Consider the tokens expired when they have 10m left
        """
        renewal_buffer = datetime.timedelta(seconds=600)
        now = datetime.datetime.utcnow()
        expiration_time = datetime.datetime.strptime(str(self.expiration),
                                                     '%Y-%m-%d %H:%M:%S+00:00')

        return (now + renewal_buffer) < expiration_time

    @property
    def is_session_valid(self):
        """Returns True if the Session is still valid.

        Takes the current time (in UTC) and compares it to the Expiration time
        returned by Amazon.
        """
        now = datetime.datetime.utcnow()
        expiration_time = datetime.datetime.strptime(str(self.expiration),
                                                     '%Y-%m-%d %H:%M:%S+00:00')

        return now < expiration_time

    def assume_role(self):
        """Use the SAML Assertion to actually get the credentials.

        Uses the supplied (one time use!) SAML Assertion to go out to Amazon
        and get back a set of temporary credentials. These are written out to
        disk and can be used for an hour before they need to be replaced.
        """
        try:
            role = self.assertion.roles()[0]
        except xml.etree.ElementTree.ParseError:
            log.error('Could not find any Role in the SAML assertion')
            log.error(self.assertion.__dict__)
            raise InvalidSaml()

        if len(self.assertion.roles()) > 1:
            log.info('More than one role available, please select one: ')
            role_count = 1
            for role in self.assertion.roles():
                print "[%s] Role: %s" % (role_count, role["role"])
                role_count += 1
            role_selection = input('Select a role from above: ')
            role_selection -= 1
            role = self.assertion.roles()[role_selection]
            self.profile = self.assertion.roles()[role_selection]["role"]

        log.info('Assuming: %s' % role["role"])
        session = self.sts.assume_role_with_saml(
            RoleArn=role['role'],
            PrincipalArn=role['principle'],
            SAMLAssertion=self.assertion.encode())
        creds = session['Credentials']

        self.aws_access_key_id = creds['AccessKeyId']
        self.aws_secret_access_key = creds['SecretAccessKey']
        self.session_token = creds['SessionToken']
        self.expiration = creds['Expiration']

        self._write()

    def _write(self):
        """Writes out our secrets to the Credentials object"""
        self.writer.add_profile(
            name="default",
            region=self.region,
            access_key=self.aws_access_key_id,
            secret_key=self.aws_secret_access_key,
            session_token=self.session_token)
        self.writer.add_profile(
            name=self.profile,
            region=self.region,
            access_key=self.aws_access_key_id,
            secret_key=self.aws_secret_access_key,
            session_token=self.session_token)
        log.info('Session expires at {time}'.format(
            time=self.expiration))
