
"""
A module for effectively caching the public keys of various token issuer endpoints.
"""

import os
import sqlite3
import time
import pkg_resources  # part of setuptools
import pwd
import re
try:
    PKG_VERSION = pkg_resources.require("scitokens")[0].version
except pkg_resources.DistributionNotFound as error:
    # During testing, scitokens won't be installed, so requiring it will fail
    # Instead, fake it
    PKG_VERSION = '1.0.0'

try:
    import urllib.request as request
except ImportError:
    import urllib2 as request

try:
    import urlparse
except ImportError:
    import urllib.parse as urlparse

import json

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_public_key
import cryptography.hazmat.backends as backends
import cryptography.hazmat.primitives.asymmetric.ec as ec
import cryptography.hazmat.primitives.asymmetric.rsa as rsa
from scitokens.utils.errors import MissingKeyException, NonHTTPSIssuer, UnableToCreateCache
from scitokens.utils import long_from_bytes


CACHE_FILENAME = "scitokens_keycache.sqllite"
KEYCACHE_INSTANCE = None

class UnableToWriteKeyCache(Exception):
    """
    For whatever reason, unable to write to the Key Cache
    """
    pass

class KeyCache(object):

    def __init__(self):
        # Check for the cache
        self.cache_location = self._get_cache_file()

    @staticmethod
    def getinstance():
        """
        Return the singleton instance of the KeyCache.
        """
        global KEYCACHE_INSTANCE
        if KEYCACHE_INSTANCE is None:
            KEYCACHE_INSTANCE = KeyCache()
        return KEYCACHE_INSTANCE

    def addkeyinfo(self, issuer, key_id, public_key, cache_timer=0):
        """
        Add a single, known public key to the cache.
        
        :param str issuer: URI of the issuer
        :param str key_id: Key Identifier
        :param public_key: Cryptography public_key object
        :param int cache_timer: Cache lifetime of the public_key
        """
        conn = sqlite3.connect(self.cache_location)
        conn.row_factory = sqlite3.Row
        curs = conn.cursor()
        curs.execute("DELETE FROM keycache WHERE issuer = '{}' AND key_id = '{}'".format(issuer, key_id))
        KeyCache._addkeyinfo(curs, issuer, key_id, public_key, cache_timer=cache_timer)
        conn.commit()
        conn.close()

    @staticmethod
    def _addkeyinfo(curs, issuer, key_id, public_key, cache_timer=0):
        """
        Given an open database cursor to a key cache, insert a key.
        """
        # Add the key to the cache
        insert_key_statement = "INSERT INTO keycache VALUES('{issuer}', '{expiration}', '{key_id}', '{keydata}')"
        keydata = public_key.public_bytes(Encoding.PEM, PublicFormat.PKCS1).decode('ascii')

        curs.execute(insert_key_statement.format(issuer=issuer, expiration=time.time()+cache_timer, key_id=key_id,
                                                 keydata=keydata))
        if curs.rowcount != 1:
            raise UnableToWriteKeyCache("Unable to insert into key cache")

    def getkeyinfo(self, issuer, key_id=None, insecure=False):
        """
        Get the key information
        
        :param str issuer: The issuer URI
        :param str key_id: Text key id to identify the key
        :returns: None if no key is found.  Else, returns the public key
        """
        # Check the sql database 
        key_query = ("SELECT * FROM keycache WHERE "
                     "issuer = '{issuer}'")
        if key_id != None:
            key_query += " AND key_id = '{key_id}'"
        conn = sqlite3.connect(self.cache_location)
        conn.row_factory = sqlite3.Row
        curs = conn.cursor()
        curs.execute(key_query.format(issuer=issuer, key_id=key_id))
        
        row = curs.fetchone()
        if row != None:
            if self._check_validity(row):
                # Convert the PEM formatted public key to a public key object
                conn.close()
                return load_pem_public_key(row['keydata'].encode(), backend=backends.default_backend())
            else:
                # Delete the row
                curs.execute("DELETE FROM keycache WHERE issuer = '{}' AND key_id = '{}'".format(row['issuer'],
                             row['key_id']))

        # If it reaches here, then no key was found in the SQL
        # Try checking the issuer (negative cache?)
        public_key, cache_timer = self._get_issuer_publickey(issuer, key_id, insecure)

        self._addkeyinfo(curs, issuer, key_id, public_key, cache_timer)

        # Save (commit) the changes
        conn.commit()
        conn.close()
        return public_key

    @classmethod
    def _check_validity(cls, key_info):
        """
        Check the key to see if it has expired
        """
        # Make sure the key hasn't expired
        if key_info['expiration'] <= time.time():
            return False
        else:
            return True

    @staticmethod
    def _get_issuer_publickey(issuer, key_id=None, insecure=False):
        """
        :return: Tuple containing (public_key, cache_lifetime).  Cache_lifetime how 
            the public key is valid
        """
        
        # Set the user agent so Cloudflare isn't mad at us
        headers={'User-Agent': 'SciTokens/{}'.format(PKG_VERSION)}
        
        # Go to the issuer's website, and download the OAuth well known bits
        # https://tools.ietf.org/html/draft-ietf-oauth-discovery-07
        well_known_uri = ".well-known/openid-configuration"
        if not issuer.endswith("/"):
            issuer = issuer + "/"
        parsed_url = urlparse.urlparse(issuer)
        updated_url = urlparse.urljoin(parsed_url.path, well_known_uri)
        parsed_url_list = list(parsed_url)
        parsed_url_list[2] = updated_url
        meta_uri = urlparse.urlunparse(parsed_url_list)

        # Make sure the protocol is https
        if not insecure:
            parsed_url = urlparse.urlparse(meta_uri)
            if parsed_url.scheme != "https":
                raise NonHTTPSIssuer("Issuer is not over HTTPS.  RFC requires it to be over HTTPS")
        response = request.urlopen(request.Request(meta_uri, headers=headers))
        data = json.loads(response.read().decode('utf-8'))

        # Get the keys URL from the openid-configuration
        jwks_uri = data['jwks_uri']

        # Now, get the keys
        if not insecure:
            parsed_url = urlparse.urlparse(jwks_uri)
            if parsed_url.scheme != "https":
                raise NonHTTPSIssuer("jwks_uri is not over HTTPS, insecure!")
        response = request.urlopen(request.Request(jwks_uri, headers=headers))

        # Get the cache data from the headers
        cache_timer = 0
        headers = response.info()
        if "Cache-Control" in headers:
            # Parse out the max-age, if it's there.
            if "max-age" in headers['Cache-Control']:
                match = re.search(".*max-age=(\d+)", headers['Cache-Control'])
                if match:
                    cache_timer = int(match.group(1))
        # Minimum cache time of 10 minutes, no matter what the remote says
        cache_timer = min(cache_timer, 600)

        keys_data = json.loads(response.read().decode('utf-8'))
        # Loop through each key, looking for the right key id
        public_key = ""
        raw_key = None

        # If there is no kid in the header, then just take the first key?
        if key_id == None:
            if len(keys_data['keys']) != 1:
                raise NotImplementedError("No kid in header, but multiple keys in "
                                          "response from certs server.  Don't know which key to use!")
            else:
                raw_key = keys_data['keys'][0]
        else:
            # Find the right key
            for key in keys_data['keys']:
                if key['kid'] == key_id:
                    raw_key = key
                    break

        if raw_key == None:
            raise MissingKeyException("Unable to find key at issuer {}".format(jwks_uri))

        if raw_key['kty'] == "RSA":
            public_key_numbers = rsa.RSAPublicNumbers(
                long_from_bytes(raw_key['e']),
                long_from_bytes(raw_key['n'])
            )
            public_key = public_key_numbers.public_key(backends.default_backend())
        elif raw_key['kty'] == 'EC':
            public_key_numbers = ec.EllipticCurvePublicNumbers(
                   long_from_bytes(raw_key['x']),
                   long_from_bytes(raw_key['y']),
                   ec.SECP256R1
               )
            public_key = public_key_numbers.public_key(backends.default_backend())
        else:
            raise UnsupportedKeyException("SciToken signed with an unsupported key type")

        return public_key, cache_timer


    def _get_cache_file(self):
        """
        Get the Cache file location
        
        1. $XDG_CACHE_HOME
        2. Home directory as returned by the password database
        """

        xdg_cache_home = os.environ.get("XDG_CACHE_HOME", None)
        home_dir = pwd.getpwuid(os.geteuid()).pw_dir

        if xdg_cache_home != None:
            cache_dir = xdg_cache_home
        elif home_dir != None:
            cache_dir = os.path.join(home_dir, ".cache")

        if not os.path.exists(cache_dir):
            try:
                os.makedirs(cache_dir)
            except OSError as ose:
                raise UnableToCreateCache("Unable to create cache: {}".format(str(ose)))

        keycache_dir = os.path.join(cache_dir, "scitokens")
        if not os.path.exists(keycache_dir):
            os.makedirs(keycache_dir)

        keycache_file = os.path.join(keycache_dir, CACHE_FILENAME)
        if not os.path.exists(keycache_file):
            self._initialize_cachedb(keycache_file)

        return keycache_file

    @staticmethod
    def _initialize_cachedb(sql_file):
        """
        Create a simple flat sqllite cache
        """
        conn = sqlite3.connect(sql_file)
        curs = conn.cursor()

        # Create cache table
        curs.execute ("CREATE TABLE keycache ("
                      "issuer text NOT NULL,"
                      "expiration integer NOT NULL,"
                      "key_id text,"
                      "keydata text NOT NULL,"
                      "PRIMARY KEY (issuer, key_id))")
        # Save (commit) the changes
        conn.commit()

        # We can also close the connection if we are done with it.
        # Just be sure any changes have been committed or they will be lost.
        conn.close()
