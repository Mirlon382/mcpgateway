from fastmcp.server import create_proxy
from fastmcp.server.transforms.search import BM25SearchTransform
from config import config, servers_metadata
#https://gofastmcp.com/servers/providers/proxy#multi-server-proxies

# réduire latence https://gofastmcp.com/servers/providers/proxy#session-reuse-for-stateless-backends - increase ttl to 1800 (30min)?

# ajouter des tools : https://gofastmcp.com/servers/providers/proxy#adding-proxied-components-to-existing-server

instruction_text="Proxy pour l'ensemble des MCP du Service Statistique Public en France. Les tools sont toujours suffixés par le serveur d'origine" + str(servers_metadata)

gateway = create_proxy(
    config,
    name="SSP Gateway",
    instructions=instruction_text
)

gateway.add_transform(BM25SearchTransform(max_results=5))

def main():
    gateway.run(
            transport="http",
            host="0.0.0.0",
            port=8000,
        )

if __name__ == "__main__":
    main()