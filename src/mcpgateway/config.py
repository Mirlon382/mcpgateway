config = {
    "mcpServers": {
        "mcpdiffusion": {
            "url": "https://mcpdiffusion.lab.sspcloud.fr/mcp",
            "transport": "http",
        },
        "geodata": {
            "url": "https://geollm.beta.ign.fr/geocontext/mcp",
            "transport": "http",
        },
    }
}

servers_metadata = {
        "mcpdiffusion": {
            "description": "Serveur pour exposer Melodi, Rmes et insee.fr",
            "tags": {"dataset", "publications"},
        },
        "geodata": {
            "description": "Données géographiques de l'IGN",
            "tags": "carte",
        }}