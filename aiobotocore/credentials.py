import asyncio
import logging
import subprocess
from copy import deepcopy
from typing import Optional

from botocore import UNSIGNED
import botocore.compat
from botocore.credentials import EnvProvider, Credentials, RefreshableCredentials, \
    ReadOnlyCredentials, ContainerProvider, ContainerMetadataFetcher, \
    _parse_if_needed, InstanceMetadataProvider, _get_client_creator, \
    ProfileProviderBuilder, ConfigProvider, SharedCredentialProvider, \
    ProcessProvider, AssumeRoleWithWebIdentityProvider, _local_now, \
    CachedCredentialFetcher, _serialize_if_needed, BaseAssumeRoleCredentialFetcher, \
    AssumeRoleProvider, AssumeRoleCredentialFetcher, CredentialResolver, \
    CanonicalNameCredentialSourcer, BotoProvider, OriginalEC2Provider
from botocore.exceptions import MetadataRetrievalError, CredentialRetrievalError, \
    InvalidConfigError, PartialCredentialsError, RefreshWithMFAUnsupportedError, \
    UnknownCredentialError
from botocore.compat import compat_shell_split

from aiobotocore.utils import AioContainerMetadataFetcher, AioInstanceMetadataFetcher
from aiobotocore.config import AioConfig

logger = logging.getLogger(__name__)


def create_credential_resolver(session, cache=None, region_name=None):
    """Create a default credential resolver.
        This creates a pre-configured credential resolver
        that includes the default lookup chain for
        credentials.
        """
    profile_name = session.get_config_variable('profile') or 'default'
    metadata_timeout = session.get_config_variable('metadata_service_timeout')
    num_attempts = session.get_config_variable('metadata_service_num_attempts')
    disable_env_vars = session.instance_variables().get('profile') is not None

    if cache is None:
        cache = {}

    env_provider = AioEnvProvider()
    container_provider = AioContainerProvider()
    instance_metadata_provider = AioInstanceMetadataProvider(
        iam_role_fetcher=AioInstanceMetadataFetcher(
            timeout=metadata_timeout,
            num_attempts=num_attempts,
            user_agent=session.user_agent())
    )

    profile_provider_builder = AioProfileProviderBuilder(
        session, cache=cache, region_name=region_name)
    assume_role_provider = AioAssumeRoleProvider(
        load_config=lambda: session.full_config,
        client_creator=_get_client_creator(session, region_name),
        cache=cache,
        profile_name=profile_name,
        credential_sourcer=AioCanonicalNameCredentialSourcer([
            env_provider, container_provider, instance_metadata_provider
        ]),
        profile_provider_builder=profile_provider_builder,
    )

    pre_profile = [
        env_provider,
        assume_role_provider,
    ]
    profile_providers = profile_provider_builder.providers(
        profile_name=profile_name,
        disable_env_vars=disable_env_vars,
    )
    post_profile = [
        AioOriginalEC2Provider(),
        AioBotoProvider(),
        container_provider,
        instance_metadata_provider,
    ]
    providers = pre_profile + profile_providers + post_profile

    if disable_env_vars:
        # An explicitly provided profile will negate an EnvProvider.
        # We will defer to providers that understand the "profile"
        # concept to retrieve credentials.
        # The one edge case if is all three values are provided via
        # env vars:
        # export AWS_ACCESS_KEY_ID=foo
        # export AWS_SECRET_ACCESS_KEY=bar
        # export AWS_PROFILE=baz
        # Then, just like our client() calls, the explicit credentials
        # will take precedence.
        #
        # This precedence is enforced by leaving the EnvProvider in the chain.
        # This means that the only way a "profile" would win is if the
        # EnvProvider does not return credentials, which is what we want
        # in this scenario.
        providers.remove(env_provider)
        logger.debug('Skipping environment variable credential check'
                     ' because profile name was explicitly set.')

    resolver = AioCredentialResolver(providers=providers)
    return resolver


class AioProfileProviderBuilder(ProfileProviderBuilder):
    def _create_process_provider(self, profile_name):
        return AioProcessProvider(
            profile_name=profile_name,
            load_config=lambda: self._session.full_config,
        )

    def _create_shared_credential_provider(self, profile_name):
        credential_file = self._session.get_config_variable('credentials_file')
        return AioSharedCredentialProvider(
            profile_name=profile_name,
            creds_filename=credential_file,
        )

    def _create_config_provider(self, profile_name):
        config_file = self._session.get_config_variable('config_file')
        return AioConfigProvider(
            profile_name=profile_name,
            config_filename=config_file,
        )

    def _create_web_identity_provider(self, profile_name, disable_env_vars):
        return AioAssumeRoleWithWebIdentityProvider(
            load_config=lambda: self._session.full_config,
            client_creator=_get_client_creator(
                self._session, self._region_name),
            cache=self._cache,
            profile_name=profile_name,
            disable_env_vars=disable_env_vars,
        )


async def get_credentials(session):
    resolver = create_credential_resolver(session)
    return await resolver.load_credentials()


def create_assume_role_refresher(client, params):
    async def refresh():
        async with client as sts:
            response = await sts.assume_role(**params)
        credentials = response['Credentials']
        # We need to normalize the credential names to
        # the values expected by the refresh creds.
        return {
            'access_key': credentials['AccessKeyId'],
            'secret_key': credentials['SecretAccessKey'],
            'token': credentials['SessionToken'],
            'expiry_time': _serialize_if_needed(credentials['Expiration']),
        }
    return refresh


def create_aio_mfa_serial_refresher(actual_refresh):
    class _Refresher(object):
        def __init__(self, refresh):
            self._refresh = refresh
            self._has_been_called = False

        async def call(self):
            if self._has_been_called:
                # We can explore an option in the future to support
                # reprompting for MFA, but for now we just error out
                # when the temp creds expire.
                raise RefreshWithMFAUnsupportedError()
            self._has_been_called = True
            return await self._refresh()

    return _Refresher(actual_refresh).call


class AioCredentials(Credentials):
    async def get_frozen_credentials(self):
        return ReadOnlyCredentials(self.access_key,
                                   self.secret_key,
                                   self.token)

    @classmethod
    def from_credentials(cls, obj: Optional[Credentials]):
        if obj is None:
            return None
        return cls(
            obj.access_key, obj.secret_key,
            obj.token, obj.method)


class AioRefreshableCredentials(RefreshableCredentials):
    def __init__(self, *args, **kwargs):
        super(AioRefreshableCredentials, self).__init__(*args, **kwargs)
        self._refresh_lock = asyncio.Lock()

    @classmethod
    def from_refreshable_credentials(cls, obj: Optional[RefreshableCredentials]):
        if obj is None:
            return None
        return cls(  # Using interval values here to skip property calling .refresh()
            obj._access_key, obj._secret_key,
            obj._token, obj._expiry_time,
            obj._refresh_using, obj.method,
            obj._time_fetcher
        )

    # Redeclaring the properties so it doesnt call refresh
    # Have to redeclare setter as we're overriding the getter
    @property
    def access_key(self):
        # TODO: this needs to be resolved
        raise NotImplementedError("missing call to self._refresh. "
                                  "Use get_frozen_credentials instead")
        return self._access_key

    @access_key.setter
    def access_key(self, value):
        self._access_key = value

    @property
    def secret_key(self):
        # TODO: this needs to be resolved
        raise NotImplementedError("missing call to self._refresh. "
                                  "Use get_frozen_credentials instead")
        return self._secret_key

    @secret_key.setter
    def secret_key(self, value):
        self._secret_key = value

    @property
    def token(self):
        # TODO: this needs to be resolved
        raise NotImplementedError("missing call to self._refresh. "
                                  "Use get_frozen_credentials instead")
        return self._token

    @token.setter
    def token(self, value):
        self._token = value

    async def _refresh(self):
        if not self.refresh_needed(self._advisory_refresh_timeout):
            return

        # By this point we need a refresh but its not critical
        if not self._refresh_lock.locked():
            async with self._refresh_lock:
                if not self.refresh_needed(self._advisory_refresh_timeout):
                    return
                is_mandatory_refresh = self.refresh_needed(
                    self._mandatory_refresh_timeout)
                await self._protected_refresh(is_mandatory=is_mandatory_refresh)
                return
        elif self.refresh_needed(self._mandatory_refresh_timeout):
            # If we're here, we absolutely need a refresh and the
            # lock is held so wait for it
            async with self._refresh_lock:
                # Might have refreshed by now
                if not self.refresh_needed(self._mandatory_refresh_timeout):
                    return
                await self._protected_refresh(is_mandatory=True)

    async def _protected_refresh(self, is_mandatory):
        try:
            metadata = await self._refresh_using()
        except Exception:
            period_name = 'mandatory' if is_mandatory else 'advisory'
            logger.warning("Refreshing temporary credentials failed "
                           "during %s refresh period.",
                           period_name, exc_info=True)
            if is_mandatory:
                # If this is a mandatory refresh, then
                # all errors that occur when we attempt to refresh
                # credentials are propagated back to the user.
                raise
            # Otherwise we'll just return.
            # The end result will be that we'll use the current
            # set of temporary credentials we have.
            return
        self._set_from_data(metadata)
        self._frozen_credentials = ReadOnlyCredentials(
            self._access_key, self._secret_key, self._token)
        if self._is_expired():
            msg = ("Credentials were refreshed, but the "
                   "refreshed credentials are still expired.")
            logger.warning(msg)
            raise RuntimeError(msg)

    async def get_frozen_credentials(self):
        await self._refresh()
        return self._frozen_credentials


class AioDeferredRefreshableCredentials(AioRefreshableCredentials):
    def __init__(self, refresh_using, method, time_fetcher=_local_now):
        self._refresh_using = refresh_using
        self._access_key = None
        self._secret_key = None
        self._token = None
        self._expiry_time = None
        self._time_fetcher = time_fetcher
        self._refresh_lock = asyncio.Lock()
        self.method = method
        self._frozen_credentials = None

    def refresh_needed(self, refresh_in=None):
        if self._frozen_credentials is None:
            return True
        return super(AioDeferredRefreshableCredentials, self).refresh_needed(
            refresh_in
        )


class AioCachedCredentialFetcher(CachedCredentialFetcher):
    async def _get_credentials(self):
        raise NotImplementedError('_get_credentials()')

    async def fetch_credentials(self):
        return await self._get_cached_credentials()

    async def _get_cached_credentials(self):
        """Get up-to-date credentials.

        This will check the cache for up-to-date credentials, calling assume
        role if none are available.
        """
        response = self._load_from_cache()
        if response is None:
            response = await self._get_credentials()
            self._write_to_cache(response)
        else:
            logger.debug("Credentials for role retrieved from cache.")

        creds = response['Credentials']
        expiration = _serialize_if_needed(creds['Expiration'], iso=True)
        return {
            'access_key': creds['AccessKeyId'],
            'secret_key': creds['SecretAccessKey'],
            'token': creds['SessionToken'],
            'expiry_time': expiration,
        }


class AioBaseAssumeRoleCredentialFetcher(BaseAssumeRoleCredentialFetcher,
                                         AioCachedCredentialFetcher):
    pass


class AioAssumeRoleCredentialFetcher(AssumeRoleCredentialFetcher,
                                     AioBaseAssumeRoleCredentialFetcher):
    async def _get_credentials(self):
        """Get credentials by calling assume role."""
        kwargs = self._assume_role_kwargs()
        client = await self._create_client()
        async with client as sts:
            return await sts.assume_role(**kwargs)

    async def _create_client(self):
        """Create an STS client using the source credentials."""
        frozen_credentials = await self._source_credentials.get_frozen_credentials()
        return self._client_creator(
            'sts',
            aws_access_key_id=frozen_credentials.access_key,
            aws_secret_access_key=frozen_credentials.secret_key,
            aws_session_token=frozen_credentials.token,
        )


class AioAssumeRoleWithWebIdentityCredentialFetcher(
        AioBaseAssumeRoleCredentialFetcher
):
    def __init__(self, client_creator, web_identity_token_loader, role_arn,
                 extra_args=None, cache=None, expiry_window_seconds=None):

        self._web_identity_token_loader = web_identity_token_loader

        super(AioAssumeRoleWithWebIdentityCredentialFetcher, self).__init__(
            client_creator, role_arn, extra_args=extra_args,
            cache=cache, expiry_window_seconds=expiry_window_seconds
        )

    async def _get_credentials(self):
        """Get credentials by calling assume role."""
        kwargs = self._assume_role_kwargs()
        # Assume role with web identity does not require credentials other than
        # the token, explicitly configure the client to not sign requests.
        config = AioConfig(signature_version=UNSIGNED)
        async with self._client_creator('sts', config=config) as client:
            return await client.assume_role_with_web_identity(**kwargs)

    def _assume_role_kwargs(self):
        """Get the arguments for assume role based on current configuration."""
        assume_role_kwargs = deepcopy(self._assume_kwargs)
        identity_token = self._web_identity_token_loader()
        assume_role_kwargs['WebIdentityToken'] = identity_token

        return assume_role_kwargs


class AioProcessProvider(ProcessProvider):
    def __init__(self, *args, popen=asyncio.create_subprocess_exec, **kwargs):
        super(AioProcessProvider, self).__init__(*args, **kwargs, popen=popen)

    async def load(self):
        credential_process = self._credential_process
        if credential_process is None:
            return

        creds_dict = await self._retrieve_credentials_using(credential_process)
        if creds_dict.get('expiry_time') is not None:
            return AioRefreshableCredentials.create_from_metadata(
                creds_dict,
                lambda: self._retrieve_credentials_using(credential_process),
                self.METHOD
            )

        return AioCredentials(
            access_key=creds_dict['access_key'],
            secret_key=creds_dict['secret_key'],
            token=creds_dict.get('token'),
            method=self.METHOD
        )

    async def _retrieve_credentials_using(self, credential_process):
        # We're not using shell=True, so we need to pass the
        # command and all arguments as a list.
        process_list = compat_shell_split(credential_process)
        p = await self._popen(process_list,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        stdout, stderr = await p.communicate()
        if p.returncode != 0:
            raise CredentialRetrievalError(
                provider=self.METHOD, error_msg=stderr.decode('utf-8'))
        parsed = botocore.compat.json.loads(stdout.decode('utf-8'))
        version = parsed.get('Version', '<Version key not provided>')
        if version != 1:
            raise CredentialRetrievalError(
                provider=self.METHOD,
                error_msg=("Unsupported version '%s' for credential process "
                           "provider, supported versions: 1" % version))
        try:
            return {
                'access_key': parsed['AccessKeyId'],
                'secret_key': parsed['SecretAccessKey'],
                'token': parsed.get('SessionToken'),
                'expiry_time': parsed.get('Expiration'),
            }
        except KeyError as e:
            raise CredentialRetrievalError(
                provider=self.METHOD,
                error_msg="Missing required key in response: %s" % e
            )


class AioInstanceMetadataProvider(InstanceMetadataProvider):
    async def load(self):
        fetcher = self._role_fetcher
        metadata = await fetcher.retrieve_iam_role_credentials()
        if not metadata:
            return None
        logger.debug('Found credentials from IAM Role: %s',
                     metadata['role_name'])

        creds = AioRefreshableCredentials.create_from_metadata(
            metadata,
            method=self.METHOD,
            refresh_using=fetcher.retrieve_iam_role_credentials,
        )
        return creds


class AioEnvProvider(EnvProvider):
    async def load(self):
        # It gets credentials from an env var,
        # so just convert the response to Aio variants
        result = super().load()
        if isinstance(result, RefreshableCredentials):
            return AioRefreshableCredentials.\
                from_refreshable_credentials(result)
        elif isinstance(result, Credentials):
            return AioCredentials.from_credentials(result)

        return None


class AioOriginalEC2Provider(OriginalEC2Provider):
    async def load(self):
        result = super(AioOriginalEC2Provider, self).load()
        if isinstance(result, Credentials):
            result = AioCredentials.from_credentials(result)
        return result


class AioSharedCredentialProvider(SharedCredentialProvider):
    async def load(self):
        result = super(AioSharedCredentialProvider, self).load()
        if isinstance(result, Credentials):
            result = AioCredentials.from_credentials(result)
        return result


class AioConfigProvider(ConfigProvider):
    async def load(self):
        result = super(AioConfigProvider, self).load()
        if isinstance(result, Credentials):
            result = AioCredentials.from_credentials(result)
        return result


class AioBotoProvider(BotoProvider):
    async def load(self):
        result = super(AioBotoProvider, self).load()
        if isinstance(result, Credentials):
            result = AioCredentials.from_credentials(result)
        return result


class AioAssumeRoleProvider(AssumeRoleProvider):
    async def load(self):
        self._loaded_config = self._load_config()
        profiles = self._loaded_config.get('profiles', {})
        profile = profiles.get(self._profile_name, {})
        if self._has_assume_role_config_vars(profile):
            return await self._load_creds_via_assume_role(self._profile_name)

    async def _load_creds_via_assume_role(self, profile_name):
        role_config = self._get_role_config(profile_name)
        source_credentials = await self._resolve_source_credentials(
            role_config, profile_name
        )

        extra_args = {}
        role_session_name = role_config.get('role_session_name')
        if role_session_name is not None:
            extra_args['RoleSessionName'] = role_session_name

        external_id = role_config.get('external_id')
        if external_id is not None:
            extra_args['ExternalId'] = external_id

        mfa_serial = role_config.get('mfa_serial')
        if mfa_serial is not None:
            extra_args['SerialNumber'] = mfa_serial

        duration_seconds = role_config.get('duration_seconds')
        if duration_seconds is not None:
            extra_args['DurationSeconds'] = duration_seconds

        fetcher = AioAssumeRoleCredentialFetcher(
            client_creator=self._client_creator,
            source_credentials=source_credentials,
            role_arn=role_config['role_arn'],
            extra_args=extra_args,
            mfa_prompter=self._prompter,
            cache=self.cache,
        )
        refresher = fetcher.fetch_credentials
        if mfa_serial is not None:
            refresher = create_aio_mfa_serial_refresher(refresher)

        # The initial credentials are empty and the expiration time is set
        # to now so that we can delay the call to assume role until it is
        # strictly needed.
        return AioDeferredRefreshableCredentials(
            method=self.METHOD,
            refresh_using=refresher,
            time_fetcher=_local_now
        )

    async def _resolve_source_credentials(self, role_config, profile_name):
        credential_source = role_config.get('credential_source')
        if credential_source is not None:
            return await self._resolve_credentials_from_source(
                credential_source, profile_name
            )

        source_profile = role_config['source_profile']
        self._visited_profiles.append(source_profile)
        return await self._resolve_credentials_from_profile(source_profile)

    async def _resolve_credentials_from_profile(self, profile_name):
        profiles = self._loaded_config.get('profiles', {})
        profile = profiles[profile_name]

        if self._has_static_credentials(profile) and \
                not self._profile_provider_builder:
            return self._resolve_static_credentials_from_profile(profile)
        elif self._has_static_credentials(profile) or \
                not self._has_assume_role_config_vars(profile):
            profile_providers = self._profile_provider_builder.providers(
                profile_name=profile_name,
                disable_env_vars=True,
            )
            profile_chain = AioCredentialResolver(profile_providers)
            credentials = await profile_chain.load_credentials()
            if credentials is None:
                error_message = (
                    'The source profile "%s" must have credentials.'
                )
                raise InvalidConfigError(
                    error_msg=error_message % profile_name,
                )
            return credentials

        return self._load_creds_via_assume_role(profile_name)

    def _resolve_static_credentials_from_profile(self, profile):
        try:
            return AioCredentials(
                access_key=profile['aws_access_key_id'],
                secret_key=profile['aws_secret_access_key'],
                token=profile.get('aws_session_token')
            )
        except KeyError as e:
            raise PartialCredentialsError(
                provider=self.METHOD, cred_var=str(e))

    async def _resolve_credentials_from_source(self, credential_source,
                                               profile_name):
        credentials = await self._credential_sourcer.source_credentials(
            credential_source)
        if credentials is None:
            raise CredentialRetrievalError(
                provider=credential_source,
                error_msg=(
                    'No credentials found in credential_source referenced '
                    'in profile %s' % profile_name
                )
            )
        return credentials


class AioAssumeRoleWithWebIdentityProvider(AssumeRoleWithWebIdentityProvider):
    async def load(self):
        return await self._assume_role_with_web_identity()

    async def _assume_role_with_web_identity(self):
        token_path = self._get_config('web_identity_token_file')
        if not token_path:
            return None
        token_loader = self._token_loader_cls(token_path)

        role_arn = self._get_config('role_arn')
        if not role_arn:
            error_msg = (
                'The provided profile or the current environment is '
                'configured to assume role with web identity but has no '
                'role ARN configured. Ensure that the profile has the role_arn'
                'configuration set or the AWS_ROLE_ARN env var is set.'
            )
            raise InvalidConfigError(error_msg=error_msg)

        extra_args = {}
        role_session_name = self._get_config('role_session_name')
        if role_session_name is not None:
            extra_args['RoleSessionName'] = role_session_name

        fetcher = AioAssumeRoleWithWebIdentityCredentialFetcher(
            client_creator=self._client_creator,
            web_identity_token_loader=token_loader,
            role_arn=role_arn,
            extra_args=extra_args,
            cache=self.cache,
        )
        # The initial credentials are empty and the expiration time is set
        # to now so that we can delay the call to assume role until it is
        # strictly needed.
        return AioDeferredRefreshableCredentials(
            method=self.METHOD,
            refresh_using=fetcher.fetch_credentials,
        )


class AioCanonicalNameCredentialSourcer(CanonicalNameCredentialSourcer):
    async def source_credentials(self, source_name):
        """Loads source credentials based on the provided configuration.

        :type source_name: str
        :param source_name: The value of credential_source in the config
            file. This is the canonical name of the credential provider.

        :rtype: Credentials
        """
        source = self._get_provider(source_name)
        if isinstance(source, AioCredentialResolver):
            return await source.load_credentials()
        return await source.load()

    def _get_provider(self, canonical_name):
        """Return a credential provider by its canonical name.

        :type canonical_name: str
        :param canonical_name: The canonical name of the provider.

        :raises UnknownCredentialError: Raised if no
            credential provider by the provided name
            is found.
        """
        provider = self._get_provider_by_canonical_name(canonical_name)

        # The AssumeRole provider should really be part of the SharedConfig
        # provider rather than being its own thing, but it is not. It is
        # effectively part of both the SharedConfig provider and the
        # SharedCredentials provider now due to the way it behaves.
        # Therefore if we want either of those providers we should return
        # the AssumeRole provider with it.
        if canonical_name.lower() in ['sharedconfig', 'sharedcredentials']:
            assume_role_provider = self._get_provider_by_method('assume-role')
            if assume_role_provider is not None:
                # The SharedConfig or SharedCredentials provider may not be
                # present if it was removed for some reason, but the
                # AssumeRole provider could still be present. In that case,
                # return the assume role provider by itself.
                if provider is None:
                    return assume_role_provider

                # If both are present, return them both as a
                # CredentialResolver so that calling code can treat them as
                # a single entity.
                return AioCredentialResolver([assume_role_provider, provider])

        if provider is None:
            raise UnknownCredentialError(name=canonical_name)

        return provider


class AioContainerProvider(ContainerProvider):
    def __init__(self, *args, **kwargs):
        super(AioContainerProvider, self).__init__(*args, **kwargs)

        # This will always run if no fetcher arg is provided
        if isinstance(self._fetcher, ContainerMetadataFetcher):
            self._fetcher = AioContainerMetadataFetcher()

    async def load(self):
        if self.ENV_VAR in self._environ or self.ENV_VAR_FULL in self._environ:
            return await self._retrieve_or_fail()

    async def _retrieve_or_fail(self):
        if self._provided_relative_uri():
            full_uri = self._fetcher.full_url(self._environ[self.ENV_VAR])
        else:
            full_uri = self._environ[self.ENV_VAR_FULL]
        headers = self._build_headers()
        fetcher = self._create_fetcher(full_uri, headers)
        creds = await fetcher()
        return AioRefreshableCredentials(
            access_key=creds['access_key'],
            secret_key=creds['secret_key'],
            token=creds['token'],
            method=self.METHOD,
            expiry_time=_parse_if_needed(creds['expiry_time']),
            refresh_using=fetcher,
        )

    def _create_fetcher(self, full_uri, headers):
        async def fetch_creds():
            try:
                response = await self._fetcher.retrieve_full_uri(
                    full_uri, headers=headers)
            except MetadataRetrievalError as e:
                logger.debug("Error retrieving container metadata: %s", e,
                             exc_info=True)
                raise CredentialRetrievalError(provider=self.METHOD,
                                               error_msg=str(e))
            return {
                'access_key': response['AccessKeyId'],
                'secret_key': response['SecretAccessKey'],
                'token': response['Token'],
                'expiry_time': response['Expiration'],
            }

        return fetch_creds


class AioCredentialResolver(CredentialResolver):
    async def load_credentials(self):
        """
        Goes through the credentials chain, returning the first ``Credentials``
        that could be loaded.
        """
        # First provider to return a non-None response wins.
        for provider in self.providers:
            logger.debug("Looking for credentials via: %s", provider.METHOD)
            creds = await provider.load()
            if creds is not None:
                return creds

        # If we got here, no credentials could be found.
        # This feels like it should be an exception, but historically, ``None``
        # is returned.
        #
        # +1
        # -js
        return None