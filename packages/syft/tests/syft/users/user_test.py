# stdlib
from secrets import token_hex

# third party
from faker import Faker
import pydantic
import pytest

# syft absolute
import syft as sy
from syft import SyftError
from syft import SyftSuccess
from syft.client.api import SyftAPICall
from syft.client.datasite_client import DatasiteClient
from syft.server.server import get_default_root_email
from syft.server.worker import Worker
from syft.service.context import AuthedServiceContext
from syft.service.user.user import ServiceRole
from syft.service.user.user import UserCreate
from syft.service.user.user import UserView

GUEST_ROLES = [ServiceRole.GUEST]
DS_ROLES = [ServiceRole.GUEST, ServiceRole.DATA_SCIENTIST]
DO_ROLES = [ServiceRole.GUEST, ServiceRole.DATA_SCIENTIST, ServiceRole.DATA_OWNER]
ADMIN_ROLES = [
    ServiceRole.GUEST,
    ServiceRole.DATA_SCIENTIST,
    ServiceRole.DATA_OWNER,
    ServiceRole.ADMIN,
]


def get_users(worker):
    return worker.get_service("UserService").get_all(
        AuthedServiceContext(server=worker, credentials=worker.signing_key.verify_key)
    )


def get_mock_client(root_client, role) -> DatasiteClient:
    worker = root_client.api.connection.server
    client = worker.guest_client
    mail = Faker().email()
    name = Faker().name()
    password = "pw"
    user = root_client.register(
        name=name, email=mail, password=password, password_verify=password
    )
    assert user
    user_id = [u for u in get_users(worker) if u.email == mail][0].id
    assert worker.root_client.api.services.user.update(uid=user_id, role=role)
    client = client.login(email=mail, password=password)
    client._fetch_api(client.credentials)
    # hacky, but useful for testing: patch user id and role on client
    client.user_id = user_id
    client.role = role
    return client


def manually_call_service(worker, client, service, args=None, kwargs=None):
    # we want to use this function because just making the call will hit the client side permissions,
    # while we mostly want to validate the server side permissions.
    args = args if args is not None else []
    kwargs = kwargs if kwargs is not None else {}
    api_call = SyftAPICall(server_uid=worker.id, path=service, args=args, kwargs=kwargs)
    signed_call = api_call.sign(client.api.signing_key)
    signed_result = client.api.connection.make_call(signed_call)
    result = signed_result.message.data
    return result


@pytest.fixture
def guest_client(worker) -> DatasiteClient:
    return get_mock_client(worker.root_client, ServiceRole.GUEST)


@pytest.fixture
def ds_client(worker) -> DatasiteClient:
    return get_mock_client(worker.root_client, ServiceRole.DATA_SCIENTIST)


@pytest.fixture
def do_client(worker) -> DatasiteClient:
    return get_mock_client(worker.root_client, ServiceRole.DATA_OWNER)


# this shadows the normal conftests.py/root_client, but I am experiencing a lot of problems
# with that fixture
@pytest.fixture
def root_client(worker):
    return get_mock_client(worker.root_client, ServiceRole.DATA_OWNER)


def test_read_user(worker, root_client, do_client, ds_client, guest_client):
    for client in [ds_client, guest_client]:
        assert not manually_call_service(worker, client, "user.get_all")

    for client in [do_client, root_client]:
        assert manually_call_service(worker, client, "user.get_all")


def test_read_returns_view(root_client):
    # Test reading returns userview (and not real user), this wasnt the case, adding this as a sanity check
    users = root_client.api.services.user
    assert len(list(users))
    for _ in users:
        # check that result has no sensitive information
        assert isinstance(root_client.api.services.user[0], UserView)


def test_user_create(worker, do_client, guest_client, ds_client, root_client):
    for client in [ds_client, guest_client]:
        assert not manually_call_service(worker, client, "user.create")
    for client in [do_client, root_client]:
        user_create = UserCreate(
            email=Faker().email(), name="z", password="pw", password_verify="pw"
        )
        res = manually_call_service(
            worker, client, "user.create", args=[], kwargs={**user_create}
        )
        assert isinstance(res, UserView)


def test_user_delete(do_client, guest_client, ds_client, worker, root_client):
    # admins can delete lower users
    clients = [get_mock_client(root_client, role) for role in DO_ROLES]
    for c in clients:
        assert worker.root_client.api.services.user.delete(c.user_id)

    # admins can delete other admins
    assert worker.root_client.api.services.user.delete(
        get_mock_client(root_client, ServiceRole.ADMIN).user_id
    )
    admin_client3 = get_mock_client(root_client, ServiceRole.ADMIN)

    # admins can delete themselves
    assert admin_client3.api.services.user.delete(admin_client3.user_id)

    # DOs can delete lower roles
    clients = [get_mock_client(root_client, role) for role in DS_ROLES]
    for c in clients:
        assert do_client.api.services.user.delete(c.user_id)
    # but not higher or same roles
    clients = [
        get_mock_client(root_client, role)
        for role in [ServiceRole.DATA_OWNER, ServiceRole.ADMIN]
    ]
    for c in clients:
        assert not do_client.api.services.user.delete(c.user_id)

    # DS cannot delete anything
    clients = [get_mock_client(root_client, role) for role in ADMIN_ROLES]
    for c in clients:
        assert not ds_client.api.services.user.delete(c.user_id)

    # Guests cannot delete anything
    clients = [get_mock_client(root_client, role) for role in ADMIN_ROLES]
    for c in clients:
        assert not guest_client.api.services.user.delete(c.user_id)


def test_user_update_roles(do_client, guest_client, ds_client, root_client, worker):
    # admins can update the roles of lower roles
    clients = [get_mock_client(root_client, role) for role in DO_ROLES]
    for _c in clients:
        assert worker.root_client.api.services.user.update(
            uid=_c.user_id, role=ServiceRole.ADMIN
        )

    # DOs can update the roles of lower roles
    clients = [get_mock_client(root_client, role) for role in DS_ROLES]
    for _c in clients:
        assert do_client.api.services.user.update(
            uid=_c.user_id, role=ServiceRole.DATA_SCIENTIST
        )

    clients = [get_mock_client(root_client, role) for role in ADMIN_ROLES]

    # DOs cannot update roles to greater than / equal to own role
    for _c in clients:
        for target_role in [ServiceRole.DATA_OWNER, ServiceRole.ADMIN]:
            assert not do_client.api.services.user.update(
                uid=_c.user_id, role=target_role
            )

    # DOs cannot downgrade higher roles to lower levels
    clients = [
        get_mock_client(root_client, role)
        for role in [ServiceRole.ADMIN, ServiceRole.DATA_OWNER]
    ]
    for _c in clients:
        for target_role in DO_ROLES:
            if target_role < _c.role:
                assert not do_client.api.services.user.update(
                    uid=_c.user_id, role=target_role
                )

    # DSs cannot update any roles
    clients = [get_mock_client(root_client, role) for role in ADMIN_ROLES]
    for _c in clients:
        for target_role in ADMIN_ROLES:
            assert not ds_client.api.services.user.update(
                uid=_c.user_id, role=target_role
            )

    # Guests cannot update any roles
    clients = [get_mock_client(root_client, role) for role in ADMIN_ROLES]
    for _c in clients:
        for target_role in ADMIN_ROLES:
            assert not guest_client.api.services.user.update(
                uid=_c.user_id, role=target_role
            )


def test_user_update(root_client):
    executing_clients = [get_mock_client(root_client, role) for role in ADMIN_ROLES]
    target_clients = [get_mock_client(root_client, role) for role in ADMIN_ROLES]

    for executing_client in executing_clients:
        for target_client in target_clients:
            if executing_client.role != ServiceRole.ADMIN:
                assert not executing_client.api.services.user.update(
                    uid=target_client.user_id, name="abc"
                )
            else:
                assert executing_client.api.services.user.update(
                    uid=target_client.user_id, name="abc"
                )

        # you can update yourself
        assert executing_client.api.services.user.update(
            uid=executing_client.user_id, name=Faker().name()
        )


def test_guest_user_update_to_root_email_failed(
    root_client: DatasiteClient,
    do_client: DatasiteClient,
    guest_client: DatasiteClient,
    ds_client: DatasiteClient,
) -> None:
    default_root_email: str = get_default_root_email()
    for client in [root_client, do_client, guest_client, ds_client]:
        res = client.api.services.user.update(
            uid=client.account.id, email=default_root_email
        )
        assert isinstance(res, SyftError)
        assert res.message == "User already exists"


def test_user_view_set_password(worker: Worker, root_client: DatasiteClient) -> None:
    root_client.account.set_password("123", confirm=False)
    email = root_client.account.email
    # log in again with the wrong password
    root_client_c = worker.root_client.login(email=email, password="1234")
    assert isinstance(root_client_c, SyftError)
    # log in again with the right password
    root_client_b = worker.root_client.login(email=email, password="123")
    assert root_client_b.account == root_client.account


@pytest.mark.parametrize(
    "invalid_email",
    ["syft", "syft.com", "syft@.com"],
)
def test_user_view_set_invalid_email(
    root_client: DatasiteClient, invalid_email: str
) -> None:
    result = root_client.account.set_email(invalid_email)
    assert isinstance(result, SyftError)


@pytest.mark.parametrize(
    "valid_email_root, valid_email_ds",
    [
        ("syft@gmail.com", "syft_ds@gmail.com"),
        ("syft@openmined.com", "syft_ds@openmined.com"),
    ],
)
def test_user_view_set_email_success(
    root_client: DatasiteClient,
    ds_client: DatasiteClient,
    valid_email_root: str,
    valid_email_ds: str,
) -> None:
    result = root_client.account.set_email(valid_email_root)
    assert isinstance(result, SyftSuccess)
    result2 = ds_client.account.set_email(valid_email_ds)
    assert isinstance(result2, SyftSuccess)


def test_user_view_set_default_admin_email_failed(
    ds_client: DatasiteClient, guest_client: DatasiteClient
) -> None:
    default_root_email = get_default_root_email()
    result = ds_client.account.set_email(default_root_email)
    assert isinstance(result, SyftError)
    assert result.message == "User already exists"

    result_2 = guest_client.account.set_email(default_root_email)
    assert isinstance(result_2, SyftError)
    assert result_2.message == "User already exists"


def test_user_view_set_duplicated_email(
    root_client: DatasiteClient, ds_client: DatasiteClient, guest_client: DatasiteClient
) -> None:
    result = ds_client.account.set_email(root_client.account.email)
    result2 = guest_client.account.set_email(root_client.account.email)

    assert isinstance(result, SyftError)
    assert result.message == "User already exists"
    assert isinstance(result2, SyftError)
    assert result2.message == "User already exists"

    result3 = guest_client.account.set_email(ds_client.account.email)
    assert isinstance(result3, SyftError)
    assert result3.message == "User already exists"


def test_user_view_update_name_institution_website(
    root_client: DatasiteClient,
    ds_client: DatasiteClient,
    guest_client: DatasiteClient,
) -> None:
    result = root_client.account.update(
        name="syft", institution="OpenMined", website="https://syft.org"
    )
    assert isinstance(result, SyftSuccess)
    assert root_client.account.name == "syft"
    assert root_client.account.institution == "OpenMined"
    assert root_client.account.website == "https://syft.org"

    result2 = ds_client.account.update(name="syft2", institution="OpenMined")
    assert isinstance(result2, SyftSuccess)
    assert ds_client.account.name == "syft2"
    assert ds_client.account.institution == "OpenMined"

    result3 = guest_client.account.update(name="syft3")
    assert isinstance(result3, SyftSuccess)
    assert guest_client.account.name == "syft3"


def test_user_view_set_role(worker: Worker, guest_client: DatasiteClient) -> None:
    admin_client = get_mock_client(worker.root_client, ServiceRole.ADMIN)
    assert admin_client.account.role == ServiceRole.ADMIN
    admin_client.register(
        name="Sheldon Cooper",
        email="sheldon@caltech.edu",
        password="changethis",
        password_verify="changethis",
        institution="Caltech",
        website="https://www.caltech.edu/",
    )
    sheldon = admin_client.users[-1]
    assert (
        sheldon.syft_client_verify_key
        == admin_client.account.syft_client_verify_key
        == admin_client.verify_key
    )
    assert sheldon.role == ServiceRole.DATA_SCIENTIST
    sheldon.update(role="guest")
    assert sheldon.role == ServiceRole.GUEST
    sheldon.update(role="data_owner")
    assert sheldon.role == ServiceRole.DATA_OWNER
    # the data scientist (Sheldon) log in the datasite, he should not
    # be able to change his role, even if he is a data owner now
    ds_client = guest_client.login(email="sheldon@caltech.edu", password="changethis")
    assert (
        ds_client.account.syft_client_verify_key
        == ds_client.verify_key
        != admin_client.verify_key
    )
    assert ds_client.account.role == sheldon.role
    assert ds_client.account.role == ServiceRole.DATA_OWNER
    assert isinstance(ds_client.account.update(role="guest"), SyftError)
    assert isinstance(ds_client.account.update(role="data_scientist"), SyftError)
    # now we set sheldon's role to admin. Only now he can change his role
    sheldon.update(role="admin")
    assert sheldon.role == ServiceRole.ADMIN
    # QA: this is different than when running in the notebook
    assert len(ds_client.users.get_all()) == len(admin_client.users.get_all())
    assert isinstance(ds_client.account.update(role="guest"), SyftSuccess)
    assert isinstance(ds_client.account.update(role="admin"), SyftError)


def test_user_view_set_role_admin(faker: Faker) -> None:
    server = sy.orchestra.launch(name=token_hex(8), reset=True)
    datasite_client = server.login(email="info@openmined.org", password="changethis")
    datasite_client.register(
        name="Sheldon Cooper",
        email="sheldon@caltech.edu",
        password="changethis",
        password_verify="changethis",
        institution="Caltech",
        website="https://www.caltech.edu/",
    )
    datasite_client.register(
        name="Sheldon Cooper",
        email="sheldon2@caltech.edu",
        password="changethis",
        password_verify="changethis",
        institution="Caltech",
        website="https://www.caltech.edu/",
    )
    assert len(datasite_client.users.get_all()) == 3

    datasite_client.users[1].update(role="admin")
    ds_client = server.login(email="sheldon@caltech.edu", password="changethis")
    assert ds_client.account.role == ServiceRole.ADMIN
    assert len(ds_client.users.get_all()) == len(datasite_client.users.get_all())

    datasite_client.users[2].update(role="admin")
    ds_client_2 = server.login(email="sheldon2@caltech.edu", password="changethis")
    assert ds_client_2.account.role == ServiceRole.ADMIN
    assert len(ds_client_2.users.get_all()) == len(datasite_client.users.get_all())

    server.python_server.cleanup()
    server.land()


@pytest.mark.parametrize(
    "search_param",
    [
        ("email", "logged_in_user"),
        ("name", "logged_in_username"),
    ],
)
def test_user_search(
    root_client: DatasiteClient,
    ds_client: DatasiteClient,
    search_param: tuple[str, str],
) -> None:
    k, attr = search_param
    v = getattr(ds_client, attr)
    users = root_client.api.services.user.search(**{k: v})

    for user in users:
        assert getattr(user, k) == v


class M(pydantic.BaseModel):
    role: ServiceRole


@pytest.mark.parametrize("role", [x.name for x in ServiceRole])
class TestServiceRole:
    @staticmethod
    def test_accept_str_in_base_model(role: str) -> None:
        m = M(role=role)
        assert m.role is getattr(ServiceRole, role)

    @staticmethod
    def test_accept_str(role: str) -> None:
        assert pydantic.TypeAdapter(ServiceRole).validate_python(role) is getattr(
            ServiceRole, role
        )
