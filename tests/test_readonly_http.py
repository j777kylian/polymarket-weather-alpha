from weather_alpha.http.readonly import ReadOnlyHttpClient, ReadOnlyHttpError


def test_post_put_patch_delete_are_unavailable() -> None:
    client = ReadOnlyHttpClient()
    for method in ("post", "put", "patch", "delete"):
        assert not hasattr(client, method), f"{method} must not be exposed"


def test_request_blocks_non_get() -> None:
    client = ReadOnlyHttpClient()
    for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        try:
            client.request(method, "https://example.invalid/")
        except ReadOnlyHttpError:
            continue
        except AttributeError:
            continue
        raise AssertionError(f"{method} must be blocked")
