import asyncio

from flarefuzz_api import gatherer
from flarefuzz_api.gatherer import _links


def test_link_extraction_follows_json_objects_but_rejects_non_nasa_hosts():
    record = {
        "Project": {"links": [{"rel": "resource", "href": "/geode-py/ws/repo/resource/1"}]},
        "Experiment Hardware": {"Link": "https://www1.grc.nasa.gov/some/general-page"},
        "external": {"url": "https://example.com/data.csv"},
    }
    assert _links(record, "https://psi.nasa.gov/geode-py/ws/repo/search") == [
        "https://psi.nasa.gov/geode-py/ws/repo/resource/1"
    ]


def test_link_extraction_handles_multiple_space_separated_urls():
    record = {
        "Resources": {
            "Link": "https://psi.nasa.gov/data/a.csv https://psi.nasa.gov/data/b.csv"
        }
    }
    assert _links(record, "https://psi.nasa.gov") == [
        "https://psi.nasa.gov/data/a.csv",
        "https://psi.nasa.gov/data/b.csv",
    ]


def test_gatherer_uses_study_inventory_file_id_and_version_for_download(monkeypatch):
    class Response:
        def __init__(self, *, payload=None, content=b"", headers=None, url="", is_redirect=False):
            self._payload = payload
            self.content = content
            self.headers = headers or {}
            self.url = url
            self.is_redirect = is_redirect

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class Client:
        def __init__(self, **kwargs):
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params=None):
            self.calls.append(("GET", url, params))
            if url == gatherer.SEARCH_URL:
                return Response(
                    payload={
                        "hits": {
                            "hits": [
                                {
                                    "_id": "PSI-117",
                                    "_source": {
                                        "Accession": "PSI-117",
                                        "Title": "PSI-117 study",
                                    },
                                }
                            ]
                        }
                    },
                    url=url,
                )
            if url == f"{gatherer.INVESTIGATION_DATA_URL}/PSI-117":
                return Response(
                    payload={"studyVersion": 7},
                    headers={"content-type": "application/json"},
                    url=url,
                )
            if url == f"{gatherer.STUDIES_URL}/PSI-117/download":
                assert params == {
                    "file": "csv-file-42",
                    "version": 2,
                    "studyVersion": 7,
                    "redirect": "false",
                }
                return Response(
                    content=b"Flame diameter (mm)\n12\n",
                    headers={"content-type": "text/csv"},
                    url="https://psi.nasa.gov/studies/PSI-117/download?file=csv-file-42&version=2&studyVersion=7&redirect=false",
                )
            raise AssertionError(f"Unexpected GET URL: {url}")

        async def post(self, url, json=None):
            self.calls.append(("POST", url, json))
            if url == gatherer.FILES_URL:
                assert json == {
                    "fileType": "study",
                    "folder": "PSI-117/",
                    "obfuscationcode": "",
                    "studyId": "PSI-117",
                    "version": 7,
                }
                return Response(
                    payload={
                        "files": [
                            {
                                "id": "directory-1",
                                "key": "PSI-117/",
                                "files": [
                                    {
                                        "id": "csv-file-42",
                                        "key": "PSI-117/Flame.csv",
                                        "version": 2,
                                        "visible": True,
                                        "restricted": False,
                                    }
                                ],
                            }
                        ]
                    },
                    url=url,
                )
            raise AssertionError(f"Unexpected POST URL: {url}")

    monkeypatch.setattr(gatherer.httpx, "AsyncClient", Client)
    results = asyncio.run(gatherer.gather_csv_inventories(term="PSI-117"))

    assert len(results) == 1
    result = results[0]
    assert result.project_id == "PSI-117"
    assert result.resource_url == (
        "https://psi.nasa.gov/studies/PSI-117/download?file=csv-file-42&version=2&studyVersion=7&redirect=false"
    )
    assert result.inventory.parameters[0].canonical_name == "flame_diameter"
    assert result.inventory.parameters[0].conversion_factor == 0.001
    assert result.inventory.parameters[0].si_unit == "m"
