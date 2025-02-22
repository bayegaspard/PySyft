# stdlib
from datetime import datetime
from datetime import timedelta
import secrets
import string

# relative
from ...abstract_server import ServerType
from ...exceptions.user import UserAlreadyExistsException
from ...serde.serializable import serializable
from ...server.credentials import SyftSigningKey
from ...server.credentials import SyftVerifyKey
from ...store.document_store import DocumentStore
from ...store.linked_obj import LinkedObject
from ...types.syft_metaclass import Empty
from ...types.uid import UID
from ...util.telemetry import instrument
from ..action.action_permissions import ActionObjectPermission
from ..action.action_permissions import ActionPermission
from ..context import AuthedServiceContext
from ..context import ServerServiceContext
from ..context import UnauthedServiceContext
from ..notification.email_templates import OnBoardEmailTemplate
from ..notification.email_templates import PasswordResetTemplate
from ..notification.notification_service import CreateNotification
from ..notification.notification_service import NotificationService
from ..notifier.notifier_enums import NOTIFIERS
from ..response import SyftError
from ..response import SyftSuccess
from ..service import AbstractService
from ..service import SERVICE_TO_TYPES
from ..service import TYPE_TO_SERVICE
from ..service import service_method
from ..settings.settings import PwdTokenResetConfig
from ..settings.settings_stash import SettingsStash
from .user import User
from .user import UserCreate
from .user import UserPrivateKey
from .user import UserSearch
from .user import UserUpdate
from .user import UserView
from .user import UserViewPage
from .user import check_pwd
from .user import salt_and_hash_password
from .user import validate_password
from .user_roles import ADMIN_ROLE_LEVEL
from .user_roles import DATA_OWNER_ROLE_LEVEL
from .user_roles import DATA_SCIENTIST_ROLE_LEVEL
from .user_roles import GUEST_ROLE_LEVEL
from .user_roles import ServiceRole
from .user_roles import ServiceRoleCapability
from .user_stash import UserStash


@instrument
@serializable(canonical_name="UserService", version=1)
class UserService(AbstractService):
    store: DocumentStore
    stash: UserStash

    def __init__(self, store: DocumentStore) -> None:
        self.store = store
        self.stash = UserStash(store=store)

    @service_method(path="user.create", name="create", autosplat="user_create")
    def create(
        self, context: AuthedServiceContext, user_create: UserCreate
    ) -> UserView | SyftError:
        """Create a new user"""
        user = user_create.to(User)
        result = self.stash.get_by_email(
            credentials=context.credentials, email=user.email
        )
        if result.is_err():
            return SyftError(message=str(result.err()))
        user_exists = result.ok() is not None
        if user_exists:
            return SyftError(message=f"User already exists with email: {user.email}")

        result = self.stash.set(
            credentials=context.credentials,
            user=user,
            add_permissions=[
                ActionObjectPermission(
                    uid=user.id, permission=ActionPermission.ALL_READ
                ),
            ],
        )
        if result.is_err():
            return SyftError(message=str(result.err()))
        user = result.ok()
        return user.to(UserView)

    def forgot_password(
        self, context: UnauthedServiceContext, email: str
    ) -> SyftSuccess | SyftError:
        success_msg = (
            "If the email is valid, we sent a password "
            + "reset token to your email or a password request to the admin."
        )
        root_key = self.admin_verify_key()

        root_context = AuthedServiceContext(server=context.server, credentials=root_key)

        result = self.stash.get_by_email(credentials=root_key, email=email)

        # Isn't a valid email
        if result.is_err():
            return SyftSuccess(message=success_msg)
        user = result.ok()

        if user is None:
            return SyftSuccess(message=success_msg)

        user_role = self.get_role_for_credentials(user.verify_key)
        if user_role == ServiceRole.ADMIN:
            return SyftError(
                message="You can't request password reset for an Admin user."
            )

        # Email is valid
        # Notifications disabled
        # We should just sent a notification to the admin/user about password reset
        # Notifications Enabled
        # Instead of changing the password here, we would change it in email template generation.
        link = LinkedObject.with_context(user, context=root_context)
        notifier_service = root_context.server.get_service("notifierservice")
        # Notifier is active
        notifier = notifier_service.settings(context=root_context)
        notification_is_enabled = notifier.active
        # Email is enabled
        email_is_enabled = notifier.email_enabled
        # User Preferences allow email notification
        user_allow_email_notifications = user.notifications_enabled[NOTIFIERS.EMAIL]

        # This checks if the user will safely receive the email reset.
        not_receive_emails = (
            not notification_is_enabled
            or not email_is_enabled
            or not user_allow_email_notifications
        )

        # If notifier service is not enabled.
        if not_receive_emails:
            message = CreateNotification(
                subject="You requested password reset.",
                from_user_verify_key=root_key,
                to_user_verify_key=user.verify_key,
                linked_obj=link,
            )

            method = root_context.server.get_service_method(NotificationService.send)
            result = method(context=root_context, notification=message)

            message = CreateNotification(
                subject="User requested password reset.",
                from_user_verify_key=user.verify_key,
                to_user_verify_key=root_key,
                linked_obj=link,
            )

            result = method(context=root_context, notification=message)
            if isinstance(result, SyftError):
                return result
        else:
            # Email notification is Enabled
            # Therefore, we can directly send a message to the
            # user with its new password.
            message = CreateNotification(
                subject="You requested a password reset.",
                from_user_verify_key=root_key,
                to_user_verify_key=user.verify_key,
                linked_obj=link,
                notifier_types=[NOTIFIERS.EMAIL],
                email_template=PasswordResetTemplate,
            )

            method = root_context.server.get_service_method(NotificationService.send)
            result = method(context=root_context, notification=message)
            if isinstance(result, SyftError):
                return result

        return SyftSuccess(message=success_msg)

    @service_method(
        path="user.request_password_reset",
        name="request_password_reset",
        roles=ADMIN_ROLE_LEVEL,
    )
    def request_password_reset(
        self, context: AuthedServiceContext, uid: UID
    ) -> str | SyftError:
        result = self.stash.get_by_uid(credentials=context.credentials, uid=uid)
        if result.is_err():
            return SyftError(
                message=(
                    f"Failed to retrieve user with UID: {uid}. Error: {str(result.err())}"
                )
            )
        user = result.ok()
        if user is None:
            return SyftError(message=f"No user exists for given: {uid}")

        user_role = self.get_role_for_credentials(user.verify_key)
        if user_role == ServiceRole.ADMIN:
            return SyftError(
                message="You can't request password reset for an Admin user."
            )

        user.reset_token = self.generate_new_password_reset_token(
            context.server.settings.pwd_token_config
        )
        user.reset_token_date = datetime.now()

        result = self.stash.update(
            credentials=context.credentials, user=user, has_permission=True
        )
        if result.is_err():
            return SyftError(
                message=(
                    f"Failed to update user with UID: {uid}. Error: {str(result.err())}"
                )
            )

        return user.reset_token

    def reset_password(
        self, context: UnauthedServiceContext, token: str, new_password: str
    ) -> SyftSuccess | SyftError:
        """Resets a certain user password using a temporary token."""
        root_key = self.admin_verify_key()

        root_context = AuthedServiceContext(server=context.server, credentials=root_key)

        result = self.stash.get_by_reset_token(
            credentials=root_context.credentials, token=token
        )
        invalid_token_error = SyftError(
            message=("Failed to reset user password. Token is invalid or expired!")
        )

        if result.is_err():
            return SyftError(message="Failed to reset user password.")

        user = result.ok()

        # If token isn't found
        if user is None:
            return invalid_token_error

        now = datetime.now()
        time_difference = now - user.reset_token_date

        # If token expired
        expiration_time = root_context.server.settings.pwd_token_config.token_exp_min
        if time_difference > timedelta(minutes=expiration_time):
            return invalid_token_error

        if not validate_password(new_password):
            return SyftError(
                message="Your new password must have at least 8 \
                characters, Upper case and lower case characters\
                and at least one number."
            )

        salt, hashed = salt_and_hash_password(new_password, 12)
        user.hashed_password = hashed
        user.salt = salt

        user.reset_token = None
        user.reset_token_date = None

        result = self.stash.update(
            credentials=root_context.credentials, user=user, has_permission=True
        )
        if result.is_err():
            return SyftError(
                message=(f"Failed to update user password.  Error: {str(result.err())}")
            )
        return SyftSuccess(message="User Password updated successfully!")

    def generate_new_password_reset_token(
        self, token_config: PwdTokenResetConfig
    ) -> str:
        valid_characters = ""
        if token_config.ascii:
            valid_characters += string.ascii_letters

        if token_config.numbers:
            valid_characters += string.digits

        generated_token = "".join(
            secrets.choice(valid_characters) for _ in range(token_config.token_len)
        )

        return generated_token

    @service_method(path="user.view", name="view", roles=DATA_SCIENTIST_ROLE_LEVEL)
    def view(
        self, context: AuthedServiceContext, uid: UID
    ) -> UserView | None | SyftError:
        """Get user for given uid"""
        result = self.stash.get_by_uid(credentials=context.credentials, uid=uid)
        if result.is_ok():
            user = result.ok()
            if user is None:
                return SyftError(message=f"No user exists for given: {uid}")
            return user.to(UserView)

        return SyftError(message=str(result.err()))

    @service_method(
        path="user.get_all",
        name="get_all",
        roles=DATA_OWNER_ROLE_LEVEL,
    )
    def get_all(
        self,
        context: AuthedServiceContext,
        page_size: int | None = 0,
        page_index: int | None = 0,
    ) -> list[UserView] | UserViewPage | UserView | SyftError:
        if context.role in [ServiceRole.DATA_OWNER, ServiceRole.ADMIN]:
            result = self.stash.get_all(context.credentials, has_permission=True)
        else:
            result = self.stash.get_all(context.credentials)
        if result.is_ok():
            results = [user.to(UserView) for user in result.ok()]

            # If chunk size is defined, then split list into evenly sized chunks
            if page_size:
                total = len(results)
                results = [
                    results[i : i + page_size]
                    for i in range(0, len(results), page_size)
                ]
                # Return the proper slice using chunk_index
                if page_index is not None:
                    results = results[page_index]
                    results = UserViewPage(users=results, total=total)

            return results

        # 🟡 TODO: No user exists will happen when result.ok() is empty list
        return SyftError(message="No users exists")

    def signing_key_for_verify_key(
        self, context: AuthedServiceContext, verify_key: SyftVerifyKey
    ) -> UserPrivateKey | SyftError:
        result = self.stash.get_by_verify_key(
            credentials=self.admin_verify_key(), verify_key=verify_key
        )
        if result.is_ok():
            user = result.ok()
            if user is not None:
                return user.to(UserPrivateKey)

            return SyftError(message=f"No user exists with {verify_key}.")

        return SyftError(
            message=f"Failed to retrieve user with {verify_key} with error: {result.err()}"
        )

    def get_role_for_credentials(
        self, credentials: SyftVerifyKey | SyftSigningKey
    ) -> ServiceRole | None | SyftError:
        # they could be different
        if isinstance(credentials, SyftVerifyKey):
            result = self.stash.get_by_verify_key(
                credentials=credentials, verify_key=credentials
            )
        else:
            result = self.stash.get_by_signing_key(
                credentials=credentials, signing_key=credentials
            )
        if result.is_ok():
            # this seems weird that we get back None as Ok(None)
            user = result.ok()
            if user:
                return user.role
        return ServiceRole.GUEST

    @service_method(path="user.search", name="search", autosplat=["user_search"])
    def search(
        self,
        context: AuthedServiceContext,
        user_search: UserSearch,
        page_size: int | None = 0,
        page_index: int | None = 0,
    ) -> UserViewPage | None | list[UserView] | SyftError:
        kwargs = user_search.to_dict(exclude_empty=True)
        kwargs.pop("created_date")
        kwargs.pop("updated_date")
        kwargs.pop("deleted_date")
        if len(kwargs) == 0:
            valid_search_params = list(UserSearch.__fields__.keys())
            return SyftError(
                message=f"Invalid Search parameters. \
                Allowed params: {valid_search_params}"
            )
        result = self.stash.find_all(credentials=context.credentials, **kwargs)

        if result.is_err():
            return SyftError(message=str(result.err()))
        users = result.ok()
        results = [user.to(UserView) for user in users] if users is not None else []

        # If page size is defined, then split list into evenly sized chunks
        if page_size:
            total = len(results)
            results = [
                results[i : i + page_size] for i in range(0, len(results), page_size)
            ]

            # Return the proper slice using page_index
            if page_index is not None:
                results = results[page_index]
                results = UserViewPage(users=results, total=total)

        return results

    # @service_method(path="user.get_admin", name="get_admin", roles=GUEST_ROLE_LEVEL)
    # def get_admin(self, context: AuthedServiceContext) -> UserView:
    #     result = self.stash.admin_user()
    #     if result.is_ok():
    #         user = result.ok()
    #         if user:
    #             return user
    #     return SyftError(message=str(result.err()))

    def get_user_id_for_credentials(
        self, credentials: SyftVerifyKey
    ) -> UID | SyftError:
        result = self.stash.get_by_verify_key(
            credentials=credentials, verify_key=credentials
        )
        if result.is_ok():
            user = result.ok()
            if user:
                return user.id
            else:
                SyftError(message="User not found!")
        return SyftError(message=str(result.err()))

    @service_method(
        path="user.get_current_user", name="get_current_user", roles=GUEST_ROLE_LEVEL
    )
    def get_current_user(self, context: AuthedServiceContext) -> UserView | SyftError:
        result = self.stash.get_by_verify_key(
            credentials=context.credentials, verify_key=context.credentials
        )
        if result.is_ok():
            user = result.ok()
            if user:
                return user.to(UserView)
            else:
                SyftError(message="User not found!")
        return SyftError(message=str(result.err()))

    @service_method(
        path="user.get_by_verify_key", name="get_by_verify_key", roles=ADMIN_ROLE_LEVEL
    )
    def get_by_verify_key_endpoint(
        self, context: AuthedServiceContext, verify_key: SyftVerifyKey
    ) -> UserView | SyftError:
        result = self.stash.get_by_verify_key(
            credentials=context.credentials, verify_key=verify_key
        )
        if result.is_ok():
            user = result.ok()
            if user:
                return user.to(UserView)
            else:
                SyftError(message="User not found!")
        return SyftError(message=str(result.err()))

    @service_method(
        path="user.update",
        name="update",
        roles=GUEST_ROLE_LEVEL,
        autosplat="user_update",
    )
    def update(
        self, context: AuthedServiceContext, uid: UID, user_update: UserUpdate
    ) -> UserView | SyftError:
        updates_role = user_update.role is not Empty  # type: ignore[comparison-overlap]
        can_edit_roles = ServiceRoleCapability.CAN_EDIT_ROLES in context.capabilities()

        if updates_role and not can_edit_roles:
            return SyftError(message=f"{context.role} is not allowed to edit roles")
        if (user_update.mock_execution_permission is not Empty) and not can_edit_roles:  # type: ignore[comparison-overlap]
            return SyftError(
                message=f"{context.role} is not allowed to update permissions"
            )

        # Get user to be updated by its UID
        result = self.stash.get_by_uid(credentials=context.credentials, uid=uid)

        immutable_fields = {"created_date", "updated_date", "deleted_date"}
        updated_fields = user_update.to_dict(
            exclude_none=True, exclude_empty=True
        ).keys()

        for field_name in immutable_fields:
            if field_name in updated_fields:
                return SyftError(
                    message=f"You are not allowed to modify '{field_name}'."
                )

        if user_update.name is not Empty and user_update.name.strip() == "":  # type: ignore[comparison-overlap]
            return SyftError(message="Name can't be an empty string.")

        # check if the email already exists (with root's key)
        if user_update.email is not Empty:
            user_with_email_exists: bool = self.stash.email_exists(
                email=user_update.email
            )
            if user_with_email_exists:
                raise UserAlreadyExistsException.raise_with_context(context=context)

        if result.is_err():
            error_msg = (
                f"Failed to find user with UID: {uid}. Error: {str(result.err())}"
            )
            return SyftError(message=error_msg)

        user = result.ok()

        if user is None:
            return SyftError(message=f"No user exists for given UID: {uid}")

        if updates_role:
            if context.role == ServiceRole.ADMIN:
                # do anything
                pass
            elif (
                context.role == ServiceRole.DATA_OWNER
                and context.role > user.role
                and context.role > user_update.role
            ):
                # as a data owner, only update lower roles to < data owner
                pass
            else:
                return SyftError(
                    message=f"As a {context.role}, you are not allowed to edit {user.role} to {user_update.role}"
                )

        edits_non_role_attrs = any(
            getattr(user_update, attr) is not Empty
            for attr in user_update.to_dict()
            if attr not in ["role", "created_date", "updated_date", "deleted_date"]
        )
        if (
            edits_non_role_attrs
            and user.verify_key != context.credentials
            and ServiceRoleCapability.CAN_MANAGE_USERS not in context.capabilities()
        ):
            return SyftError(
                message=f"As a {context.role}, you are not allowed to edit users"
            )

        # Fill User Update fields that will not be changed by replacing it
        # for the current values found in user obj.
        for name, value in user_update.to_dict(exclude_empty=True).items():
            if name == "password" and value:
                salt, hashed = salt_and_hash_password(value, 12)
                user.hashed_password = hashed
                user.salt = salt
            elif not name.startswith("__") and value is not None:
                setattr(user, name, value)

        result = self.stash.update(
            credentials=context.credentials, user=user, has_permission=True
        )

        if result.is_err():
            error_msg = (
                f"Failed to update user with UID: {uid}. Error: {str(result.err())}"
            )
            return SyftError(message=error_msg)

        user = result.ok()
        if user.role == ServiceRole.ADMIN:
            settings_stash = SettingsStash(store=self.store)
            settings = settings_stash.get_all(context.credentials)
            if settings.is_ok() and len(settings.ok()) > 0:
                settings_data = settings.ok()[0]
                settings_data.admin_email = user.email
                settings_stash.update(
                    credentials=context.credentials, settings=settings_data
                )

        return user.to(UserView)

    def get_target_object(
        self, credentials: SyftVerifyKey, uid: UID
    ) -> User | SyftError:
        user_result = self.stash.get_by_uid(credentials=credentials, uid=uid)
        if user_result.is_err():
            return SyftError(message=str(user_result.err()))
        user = user_result.ok()
        if user is None:
            return SyftError(message=f"No user exists for given id: {uid}")
        else:
            return user

    @service_method(path="user.delete", name="delete", roles=GUEST_ROLE_LEVEL)
    def delete(self, context: AuthedServiceContext, uid: UID) -> bool | SyftError:
        # third party
        user = self.get_target_object(context.credentials, uid)
        if isinstance(user, SyftError):
            return user

        permission_error = SyftError(
            message=str(
                f"As a {context.role} you have no permission to delete user with {user.role} permission"
            )
        )
        if context.role == ServiceRole.DATA_OWNER and user.role in [
            ServiceRole.GUEST,
            ServiceRole.DATA_SCIENTIST,
        ]:
            pass
        elif context.role == ServiceRole.ADMIN:
            pass
        else:
            return permission_error

        result = self.stash.delete_by_uid(
            credentials=context.credentials, uid=uid, has_permission=True
        )
        if result.is_err():
            return SyftError(message=str(result.err()))

        # TODO: Remove notifications for the deleted user

        return result.ok()

    def exchange_credentials(
        self, context: UnauthedServiceContext
    ) -> UserPrivateKey | SyftError:
        """Verify user
        TODO: We might want to use a SyftObject instead
        """
        if context.login_credentials is None:
            return SyftError(message="Invalid login credentials")
        result = self.stash.get_by_email(
            credentials=self.admin_verify_key(), email=context.login_credentials.email
        )
        if result.is_ok():
            user = result.ok()
            if user is not None and check_pwd(
                context.login_credentials.password,
                user.hashed_password,
            ):
                if (
                    context.server
                    and context.server.server_type == ServerType.ENCLAVE
                    and user.role == ServiceRole.ADMIN
                ):
                    return SyftError(
                        message="Admins are not allowed to login to Enclaves."
                        "\n Kindly register a new data scientist account by your_client.register."
                    )
                return user.to(UserPrivateKey)

            return SyftError(
                message="No user exists with "
                f"{context.login_credentials.email} and supplied password."
            )

        return SyftError(
            message="Failed to retrieve user with "
            f"{context.login_credentials.email} with error: {result.err()}"
        )

    def admin_verify_key(self) -> SyftVerifyKey | SyftError:
        try:
            result = self.stash.admin_verify_key()
            if result.is_ok():
                return result.ok()
            else:
                return SyftError(message="failed to get admin verify_key")

        except Exception as e:
            return SyftError(message=str(e))

    def register(
        self, context: ServerServiceContext, new_user: UserCreate
    ) -> tuple[SyftSuccess, UserPrivateKey] | SyftError:
        """Register new user"""

        request_user_role = (
            ServiceRole.GUEST
            if new_user.created_by is None
            else self.get_role_for_credentials(new_user.created_by)
        )

        can_user_register = (
            context.server.settings.signup_enabled
            or request_user_role in DATA_OWNER_ROLE_LEVEL
        )

        if not can_user_register:
            return SyftError(
                message=f"You don't have permission to create an account "
                f"on the datasite: {context.server.name}. Please contact the Datasite Owner."
            )

        user = new_user.to(User)
        result = self.stash.get_by_email(credentials=user.verify_key, email=user.email)
        if result.is_err():
            return SyftError(message=str(result.err()))

        user_exists = result.ok() is not None
        if user_exists:
            return SyftError(message=f"User already exists with email: {user.email}")

        result = self.stash.set(
            credentials=user.verify_key,
            user=user,
            add_permissions=[
                ActionObjectPermission(
                    uid=user.id, permission=ActionPermission.ALL_READ
                ),
            ],
        )
        if result.is_err():
            return SyftError(message=str(result.err()))

        user = result.ok()

        success_message = f"User '{user.name}' successfully registered!"

        # Notification Step
        root_key = self.admin_verify_key()
        root_context = AuthedServiceContext(server=context.server, credentials=root_key)
        link = None
        if new_user.created_by:
            link = LinkedObject.with_context(user, context=root_context)
        message = CreateNotification(
            subject=success_message,
            from_user_verify_key=root_key,
            to_user_verify_key=user.verify_key,
            linked_obj=link,
            notifier_types=[NOTIFIERS.EMAIL],
            email_template=OnBoardEmailTemplate,
        )

        method = context.server.get_service_method(NotificationService.send)
        result = method(context=root_context, notification=message)

        if request_user_role in DATA_OWNER_ROLE_LEVEL:
            success_message += " To see users, run `[your_client].users`"

        # TODO: Add a notifications for the new user

        msg = SyftSuccess(message=success_message)
        return (msg, user.to(UserPrivateKey))

    def user_verify_key(self, email: str) -> SyftVerifyKey | SyftError:
        # we are bypassing permissions here, so dont use to return a result directly to the user
        credentials = self.admin_verify_key()
        result = self.stash.get_by_email(credentials=credentials, email=email)
        if result.ok() is not None:
            return result.ok().verify_key
        return SyftError(message=f"No user with email: {email}")

    def get_by_verify_key(self, verify_key: SyftVerifyKey) -> UserView | SyftError:
        # we are bypassing permissions here, so dont use to return a result directly to the user
        credentials = self.admin_verify_key()
        result = self.stash.get_by_verify_key(
            credentials=credentials, verify_key=verify_key
        )
        if result.is_ok():
            return result.ok()
        return SyftError(message=f"No User with verify_key: {verify_key}")

    # TODO: This exposed service is only for the development phase.
    # enable/disable notifications will be called from Notifier Service

    def _set_notification_status(
        self,
        notifier_type: NOTIFIERS,
        new_status: bool,
        verify_key: SyftVerifyKey,
    ) -> SyftError | None:
        result = self.stash.get_by_verify_key(
            credentials=verify_key, verify_key=verify_key
        )
        if result.is_ok():
            # this seems weird that we get back None as Ok(None)
            user = result.ok()
        else:
            return SyftError(message=str(result.err()))

        user.notifications_enabled[notifier_type] = new_status

        result = self.stash.update(
            credentials=user.verify_key,
            user=user,
        )
        if result.is_err():
            return SyftError(message=str(result.err()))
        else:
            return None

    def enable_notifications(
        self, context: AuthedServiceContext, notifier_type: NOTIFIERS
    ) -> SyftSuccess | SyftError:
        result = self._set_notification_status(notifier_type, True, context.credentials)
        if result is not None:
            return result
        else:
            return SyftSuccess(message="Notifications enabled successfully!")

    def disable_notifications(
        self, context: AuthedServiceContext, notifier_type: NOTIFIERS
    ) -> SyftSuccess | SyftError:
        result = self._set_notification_status(
            notifier_type, False, context.credentials
        )
        if result is not None:
            return result
        else:
            return SyftSuccess(message="Notifications disabled successfully!")


TYPE_TO_SERVICE[User] = UserService
SERVICE_TO_TYPES[UserService].update({User})
