# SPDX-License-Identifier: AGPL-3.0-or-later
"""Printables (3D printing models)

Searches `printables.com`_ through its public GraphQL API and returns
3D-printable models as image results.

.. _printables.com: https://www.printables.com
"""

from json import dumps
from datetime import datetime

from searx.result_types import EngineResults

# about
about = {
    "website": "https://www.printables.com",
    "wikidata_id": None,
    "official_api_documentation": None,
    "use_official_api": True,
    "require_api_key": False,
    "results": "JSON",
}

# engine dependent config
categories = ["3d print"]
paging = True
results_per_page = 20

api_url = "https://api.printables.com/graphql/"
base_url = "https://www.printables.com"
media_url = "https://media.printables.com/"

gql_query = """query SearchModels($query: String!, $limit: Int, $offset: Int, $ordering: SearchChoicesEnum) {
  result: searchPrints2(query: $query, limit: $limit, offset: $offset, ordering: $ordering) {
    items {
      id
      name
      slug
      ratingAvg
      likesCount
      datePublished
      image { filePath }
      user { publicUsername }
    }
  }
}"""


def request(query, params):
    offset = (params["pageno"] - 1) * results_per_page
    data = {
        "operationName": "SearchModels",
        "query": gql_query,
        "variables": {
            "query": query,
            "limit": results_per_page,
            "offset": offset,
            "ordering": "best_match",
        },
    }
    params["url"] = api_url
    params["method"] = "POST"
    params["headers"]["content-type"] = "application/json"
    params["data"] = dumps(data)
    return params


def response(resp) -> EngineResults:
    results = EngineResults()
    json_data = resp.json()

    result = (json_data.get("data") or {}).get("result") or {}
    for item in result.get("items", []):
        url = f"{base_url}/model/{item['id']}-{item['slug']}"

        img_src = None
        image = item.get("image") or {}
        if image.get("filePath"):
            img_src = media_url + image["filePath"]

        content = []
        author = (item.get("user") or {}).get("publicUsername")
        if item.get("ratingAvg"):
            content.append(f"★ {float(item['ratingAvg']):.1f}")
        if item.get("likesCount"):
            content.append(f"♥ {item['likesCount']}")

        published_date = None
        if item.get("datePublished"):
            try:
                published_date = datetime.fromisoformat(item["datePublished"])
            except ValueError:
                published_date = None

        results.add(
            results.types.LegacyResult(
                {
                    "template": "images.html",
                    "url": url,
                    "title": item["name"],
                    "content": " · ".join(content),
                    "author": author,
                    "img_src": img_src,
                    "thumbnail_src": img_src,
                    "publishedDate": published_date,
                }
            )
        )

    return results
