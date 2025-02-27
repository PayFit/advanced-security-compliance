import json
import requests
from string import Template
from ghascompliance.octokit.octokit import GitHub, OctoRequests, Octokit

GRAPHQL_GET_INFO = """\
{
    repository(owner: "$owner", name: "$repo") {
        vulnerabilityAlerts(first: 100, states: [OPEN]) {
            nodes {
                createdAt
                dismissReason
                securityVulnerability {
                    package {
                        ecosystem
                        name
                    }
                }
                securityAdvisory {
                    ghsaId
                    severity
                    cwes(first: 100) {
                        edges {
                            node {
                                cweId
                            }
                        }
                    }
                }
            }
        }
    }
}
"""

# https://docs.github.com/en/graphql/reference/objects#repository
# https://docs.github.com/en/graphql/reference/objects#dependencygraphdependency
GRAPHQL_DEPENDENCY_INFO = """\
query getRepoDeps($id: ID!, $after: String) {
  node(id: $id) {
    ... on Repository {
      name
      isArchived
      dependencyGraphManifests {
        pageInfo {
          endCursor
        }
        nodes {
          id
          exceedsMaxSize
          filename
          parseable
          dependenciesCount
          dependencies(first: 100, after: $after) {
            pageInfo {
              endCursor
              hasNextPage
            }
            nodes {
              packageName
              packageManager
              requirements
              repository {
                isArchived
                isDisabled
                isEmpty
                isFork
                isSecurityPolicyEnabled
                isInOrganization
                licenseInfo {
                  name
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

GRAPHQL_REPOSITORY_INFO = """\
{
    repository(owner: "$owner", name: "$repo") {
        name
        id
    }
}
"""


class Dependencies(OctoRequests):
    def __init__(self, github: GitHub):
        instance = "https://api.github.com/graphql"
        super().__init__(github=github)

        self.headers["Accept"] = "application/vnd.github.hawkgirl-preview+json"

        self.dependencies = []

    @staticmethod
    def createDependencyName(manager: str, dependency: str, version: str = None):
        """Create a dependency full name"""
        ret = manager.lower() + "://" + dependency.lower()
        if version:
            ret += "#" + version.lower()
        return ret

    def getOpenAlerts(self, response: dict = {}):
        """Get Open Security Dependencies Alerts"""

        variables = {"owner": self.github.owner, "repo": self.github.repo}

        query = Template(GRAPHQL_GET_INFO).substitute(**variables)

        request = requests.post(
            "https://api.github.com/graphql",
            json={"query": query},
            headers=self.headers,
        )

        if request.status_code != 200:
            raise Exception(
                "Query failed to run by returning code of {}. {}".format(
                    request.status_code, query
                )
            )
        response = request.json()
        if response.get("errors"):
            Octokit.error(json.dumps(response))
            raise Exception("Query failed to run")

        results = []

        data = (
            response.get("data", {})
            .get("repository", {})
            .get("vulnerabilityAlerts", {})
            .get("nodes", [])
        )
        for alert in data:
            results.append(
                {
                    "createdAt": alert.get("createdAt"),
                    "dismissReason": alert.get("dismissReason"),
                }
            )
        return data

    def getDependencies(self, response: dict = {}):
        """Get Open Dependencies"""

        variables = {"owner": self.github.owner, "repo": self.github.repo}

        has_next_page = True
        page_cursor = None

        get_repo_info_query = Template(GRAPHQL_REPOSITORY_INFO).substitute(**variables)

        repo_request = requests.post(
            "https://api.github.com/graphql",
            json={"query": get_repo_info_query},
            headers=self.headers,
            timeout=60,
        )

        if repo_request.status_code != 200:
            raise Exception(
                "Repository query failed to run by returning code of {}. {}".format(
                    repo_request.status_code, repo_request
                )
            )

        repo_response = repo_request.json()
        if repo_response.get("errors"):
            Octokit.error(json.dumps(repo_response, indent=2))
            raise Exception("Repository query failed to run")

        repository = repo_response.get("data", {}).get("repository", {})
        repository_id: str = repository.get("id", {})

        results = []
        while has_next_page:

            variables = {
                "id": repository_id,
                "after": page_cursor,
            }

            get_dependencies_query = GRAPHQL_DEPENDENCY_INFO
            dependencies_request = requests.post(
                "https://api.github.com/graphql",
                json={"query": get_dependencies_query, "variables": variables},
                headers=self.headers,
            )

            if dependencies_request.status_code != 200:
                raise Exception(
                    "Query failed to run by returning code of {}. {}".format(
                        dependencies_request.status_code, get_dependencies_query
                    )
                )
            dependencies_response = dependencies_request.json()
            if dependencies_response.get("errors"):
                Octokit.error(json.dumps(dependencies_response, indent=2))
                raise Exception("Query failed to run")

            repository = dependencies_response.get("data", {}).get("node", {})
            # repo_name = repo.get('name')
            # repo_license = repo.get('licenseInfo', {}).get('name')

            manifests = repository.get("dependencyGraphManifests", {}).get("nodes", [])
            has_next_page = False
            for manifest in manifests:
                manifest_path = manifest.get("filename")
                if manifest["dependencies"]["pageInfo"]["hasNextPage"]:
                    page_cursor = (
                        manifest.get("dependencies", {})
                        .get("pageInfo", {})
                        .get("endCursor", str)
                    )
                    has_next_page = True

                dependencies = manifest.get("dependencies", {}).get("nodes", [])
                for dependency in dependencies:
                    dependency_manager = dependency.get("packageManager", "NA").lower()
                    dependency_name = dependency.get("packageName", "NA")
                    dependency_repo = dependency.get("repository", {})
                    dependency_requirement = (
                        dependency.get("requirements", "")
                        .replace("= ", "")
                        .replace("^ ", "")
                    )

                    dependency_license = (
                        dependency_repo.get("licenseInfo") if dependency_repo else {}
                    )

                    dependency_license_name = (
                        dependency_license.get("name", "NA")
                        if dependency_license
                        else "NA"
                    )

                    Octokit.debug(f" > {dependency_name} == {dependency_license_name}")

                    dependency_maintenance = []
                    for dep_maintenance in [
                        "isArchived",
                        "isDisabled",
                        "isEmpty",
                        "isLocked",
                    ]:
                        if dependency_repo and dependency_repo.get(
                            dep_maintenance, False
                        ):
                            dependency_maintenance.append(
                                dep_maintenance.replace("is", "", 1).lower()
                            )

                    is_organization: bool = None
                    if dependency_repo:
                        is_organization = dependency_repo.get("isInOrganization")

                    full_name = Dependencies.createDependencyName(
                        dependency_manager, dependency_name, dependency_requirement
                    )

                    results.append(
                        {
                            "name": dependency_name,
                            "full_name": full_name,
                            "manager": dependency_manager,
                            "manager_path": manifest_path,
                            "version": dependency_requirement,
                            "license": dependency_license_name,
                            "maintenance": dependency_maintenance,
                            "organization": is_organization,
                        }
                    )
        self.dependencies = results
        return results
