# MCP Gateway Insee

*Rassembler les différents services MCP de l'Insee*

---
<!-- ─── BETA BADGE ───────────────────────────────────────────────────────── -->
![Status: BETA](https://img.shields.io/badge/status-BETA-yellow?style=for-the-badge)

> **⚠️  BÊTA – Ce projet est un prototype**  
> Ce projet est **un prototype**. Il ne doit **pas** être considéré comme un produit fini ou officiellement supporté par l’INSEE.

---

Passerelle [FastMCP 4](https://gofastmcp.com) qui agrège les serveurs MCP du Service
Statistique Public derrière un point d'entrée unique : un seul endpoint MCP, un
catalogue lisible, un endpoint de santé et des métriques exploitables.

## Lancer le projet

```{bash}
uv sync
uv run mcpgateway                 # HTTP sur 0.0.0.0:8000, endpoint MCP /mcp
uv run mcpgateway --help
```

Vérifier le registre sans démarrer la passerelle (utile en CI) :

```{bash}
uv run mcpgateway --check         # résumé JSON : serveurs, domaines, limites, fonctionnalités
```

Options utiles : `--registry <chemin>`, `--host`, `--port`, `--path`, `--transport`,
`--log-level`, `--text-logs` (logs lisibles au lieu du JSON).

## 1. Registre déclaratif (`registry.yaml`)

La configuration n'est plus un module Python mais un fichier YAML **validé au
démarrage** ([`src/mcpgateway/registry.py`](src/mcpgateway/registry.py)). Ajouter un
serveur est une modification de données, pas de code.

```yaml
gateway:
  name: SSP Gateway
  tool_search: {enabled: true, max_results: 5}
  catalog: {enabled: true, include_tool_inventory: true, include_guide: true, guide_path: guide-ssp.md}
  health: {enabled: true, path: /health, probe_interval_seconds: 60, failure_threshold: 2}
  metrics: {log_level: INFO, json_logs: true}
  concurrency: {global_limit: 16, acquire_timeout_seconds: 5}

servers:
  - name: mcpdiffusion                        # sert aussi de préfixe d'outils
    url: https://mcpdiffusion.lab.sspcloud.fr/mcp
    transport: http                           # http | sse
    domain: diffusion                         # regroupement fonctionnel
    description: Serveur pour exposer Melodi, Rmes et insee.fr
    tags: [dataset, publications]             # liste ou chaîne unique
    enabled: true                             # false : gardé au registre, non exposé
    timeout: 30.0                             # délai par appel amont (s)
    max_concurrency: 4                        # appels simultanés vers ce serveur
    cache_ttl: 300                            # cache des listes de composants (s)
```

Le fichier est cherché dans `--registry`, sinon `$MCPGATEWAY_REGISTRY`, sinon
`registry.yaml` / `registry.yml` / `mcpgateway.yaml` dans le répertoire courant.

**Validation** — une erreur arrête le démarrage avec un message précis plutôt que de
produire une erreur de proxy difficile à diagnostiquer :

| Cas | Comportement |
| --- | --- |
| Schéma d'URL non `http(s)`, hôte manquant | rejeté |
| Nom invalide (tirets, espaces, chiffre initial) ; majuscules | rejeté / normalisé en minuscules |
| Nom de serveur en doublon | rejeté (les noms sont des préfixes, ils doivent être uniques) |
| Clé inconnue (`transports:` au lieu de `transport:`) | rejeté (`extra="forbid"`) |
| `timeout` ≤ 0, `max_concurrency` < 1, `cache_ttl` < 0 | rejeté |
| `tags: carte` (chaîne) ou `tags: [carte]` | les deux acceptés |

`name` est aussi le préfixe de tous les composants exposés : le serveur `mcpdiffusion`
expose `mcpdiffusion_<outil>`, et ses ressources deviennent `scheme://mcpdiffusion/…`.

## 2. Santé et joignabilité des amonts

`GET /health` ([`src/mcpgateway/health.py`](src/mcpgateway/health.py)) répond à deux
questions : le processus est-il vivant, et ses amonts sont-ils joignables.

```bash
curl localhost:8000/health              # résumé : statut, compteurs, amonts injoignables
curl 'localhost:8000/health?details=1'  # + détail par amont et instantané des métriques
curl 'localhost:8000/health?refresh=1'  # sonde immédiate avant de répondre
```

* La joignabilité est mesurée par un **aller-retour réel** (`list_tools()`), pas par
  `ping` : sur le protocole sans session (`2026-07-28`) que les clients FastMCP
  négocient par défaut, `ping` n'est pas routé et lève `Method not found`.
* Une boucle de fond sonde les amonts (`probe_interval_seconds`) et mémorise état,
  latence, dernier succès, dernier échec et échecs consécutifs.
* Un amont est déclaré `down` après `failure_threshold` échecs consécutifs ; un échec
  isolé reste `degraded`, pour qu'un incident transitoire ne fasse pas redémarrer la
  passerelle.
* Codes HTTP : `200` si aucun amont n'est `down`, `503` sinon. Le corps porte
  `status` (`ok` / `degraded` / `down` / `empty`), les compteurs et `unreachable`.
* Le résumé **n'expose pas les URL** des amonts : un endpoint de santé sans
  authentification (les routes personnalisées FastMCP ne passent jamais par
  l'authentification, c'est voulu pour les sondes) n'a pas à servir d'annuaire.

## 3. Journalisation structurée des métriques

Tout événement opérationnel est une ligne JSON (`{"ts","level","logger","event",…}`,
[`src/mcpgateway/logging_utils.py`](src/mcpgateway/logging_utils.py)) :

```json
{"ts":"…","level":"INFO","event":"tool_call","upstream":"mcpdiffusion","tool":"mcpdiffusion_search_melodi_datasets",
 "duration_ms":412.7,"status":"ok"}
{"ts":"…","level":"INFO","event":"upstream_call","upstream":"geodata","operation":"probe","duration_ms":918.46,
 "status":"ok","tool_count":10,"url":"https://geollm.beta.ign.fr/geocontext/mcp"}
{"ts":"…","level":"WARNING","event":"health_check","status":"degraded","http_status":503,"servers":{…}}
```

Événements émis : `tool_call`, `upstream_call` (`probe` / `inventory` / `read_resource` /
`get_prompt` / `list_tools`), `health_check`, `catalog_read`, `concurrency_rejected`,
`probe_cycle`, `gateway_started` / `gateway_stopped` / `gateway_assembled`.

Métriques conservées en mémoire ([`src/mcpgateway/metrics.py`](src/mcpgateway/metrics.py)) :
compteurs, erreurs et latences (moyenne / min / max / p50 / p95) par `(amont, outil)`
pour les appels d'outils et par `(amont, opération)` pour les appels sortants. Elles
sont lisibles via `/health?details=1` et `catalog://servers/{nom}`. `--text-logs`
bascule sur un format lisible qui conserve les mêmes champs.

## 4. Délais et limites de concurrence

[`src/mcpgateway/middleware.py`](src/mcpgateway/middleware.py) et
[`src/mcpgateway/upstreams.py`](src/mcpgateway/upstreams.py) :

* **délai par appel amont** — `servers[].timeout`, appliqué au client proxy et aux
  opérations propres à la passerelle (sonde, inventaire) ;
* **limite par amont** — `servers[].max_concurrency`, sémaphore partagé par les appels
  d'outils proxifiés *et* par les sondes/inventaires ;
* **plafond global** — `gateway.concurrency.global_limit` ;
* **attente bornée** — au-delà de `acquire_timeout_seconds`, l'appel reçoit une erreur
  explicite (`Gateway is busy: '<amont>' refused …`) au lieu de patienter
  indéfiniment ; l'événement `concurrency_rejected` est journalisé ;
* les créneaux sont toujours libérés (y compris sur exception) et l'attente est
  comptabilisée séparément comme opération `slot_wait`.

## 5. Catalogue en ressources MCP

[`src/mcpgateway/catalog.py`](src/mcpgateway/catalog.py) — optionnel, activé par
`gateway.catalog.enabled`, l'inventaire des outils par
`gateway.catalog.include_tool_inventory` :

| Ressource | Contenu |
| --- | --- |
| `catalog://servers` | tous les serveurs : domaine, description, tags, `enabled`, préfixe, limites, état de joignabilité |
| `catalog://domains` | serveurs regroupés par domaine fonctionnel |
| `catalog://servers/{name}` | détail d'un serveur + ses métriques (outils, sondes) |
| `catalog://servers/{name}/tools` | inventaire des outils : nom amont, **nom à appeler** (`{serveur}_{outil}`), description, paramètres |
| `catalog://guide` | guide d'utilisation : mécanique de la passerelle, puis les démarches propres au déploiement |

Le catalogue est exposé en **ressources** et non en outils : cela ne coûte rien au
contexte du client tant qu'il ne lit pas, et cela reste lisible quand la recherche
d'outils masque `tools/list`. Un amont injoignable ne casse pas la ressource : elle
répond `status: "unavailable"` avec l'erreur.

> **Limite connue** — la recherche d'outils (BM25) indexe les noms, descriptions et
> paramètres des outils, pas le vocabulaire du registre. Une requête comme `diffusion`
> peut donc ne rien renvoyer alors que le domaine existe : les instructions générées et
> `catalog://domains` restent la voie de découverte par domaine.

### Le guide (`catalog://guide`)

Le guide est en deux couches, dans une seule ressource :

1. **la mécanique, fournie par le paquet** (`src/mcpgateway/guide.md`) — nommage
   `{serveur}_{outil}`, catalogue masqué derrière `search_tools`/`call_tool`, ressources
   du catalogue, pièges courants. Elle ne cite **aucun** serveur ni outil de ce
   déploiement : elle reste donc vraie quel que soit le registre, et une mise à jour du
   paquet la corrige.
2. **les démarches du déploiement** (`guide-ssp.md`, via `gateway.catalog.guide_path`),
   ajoutées après un séparateur — workflows multi-serveurs, noms d'outils réels, spécificités
   des amonts.

Le guide de déploiement contient du **récit et des renvois**, jamais d'inventaire : celui-ci
reste généré (`instructions`, `catalog://servers*`), pour éviter deux sources de vérité.
`guide_path` est relatif au fichier de registre et vérifié au démarrage ; les deux fichiers
sont relus à chaque lecture, donc éditables sans redémarrage. `include_guide: false` retire
la ressource (et la mention dans les instructions).

Ce contenu est le même fichier qui pourra être exposé via l'API *skills* de FastMCP (le
frontmatter `name`/`description` est déjà présent) quand les clients sauront lire
`skills/list` — aujourd'hui `SkillsClientExtension` n'existe pas dans la version publiée.

Deux garde-fous empêchent la dérive du guide de déploiement : un test hermétique vérifie
que chaque serveur cité est bien activé dans le registre, et un test d'intégration vérifie
que **chaque outil cité existe encore** dans l'inventaire réel des amonts (il est ignoré si
un amont est injoignable).

## Empreinte sur le contexte

Un serveur MCP consomme du contexte **au moment de la connexion**, avant que le modèle
n'ait rien demandé. Les chiffres ci-dessous sont mesurés, pas estimés :

```bash
uv run python scripts/measure_context.py        # registre réel : 22 outils amont
```

| Charge utile | Recherche d'outils activée | désactivée |
| --- | --- | --- |
| `instructions` | 856 o | 722 o |
| `tools/list` | 1 374 o (2 outils : `search_tools`, `call_tool`) | **59 241 o (22 outils)** |
| `resources/list` | 917 o (3 ressources, en-têtes seuls) | 917 o |
| `resources/templates/list` | 687 o (2 gabarits) | 687 o |
| **Total à la connexion** | **3,7 Ko** | **60,1 Ko** |

Soit un facteur **×16,1** entre les deux configurations : masquer le catalogue derrière
la recherche est le levier le plus rentable de toute la liste, et c'est pourquoi
`tool_search.enabled` est à `true`.

Le coût de l'agrégation, lui, ne dépend pas de ce réglage : chaque amont est sondé et son
inventaire lu à la connexion (mesuré : `mcpdiffusion` 206–346 ms, `geodata` 813–968 ms,
en parallèle et borné par `timeout`). C'est ce coût qui augmente si l'on ajoute des
serveurs amont — pas la taille envoyée au modèle.

### Ce qui ne part *pas* à la connexion

* **Le contenu des ressources.** `resources/list` ne renvoie que les en-têtes (nom, URI,
  description, type MIME) ; le texte n'est transféré que sur un `resources/read` explicite.
  Mesuré : `catalog://guide` = 8 302 caractères, soit 2,2× tout ce qui part à la connexion,
  et uniquement si le client le lit.
* **Le contenu des prompts.** Les prompts sont à *contrôle utilisateur* dans le protocole :
  ils apparaissent dans l'interface, pas dans le contexte du modèle.
* **Les schémas des outils masqués.** Avec la recherche, le client ne reçoit que les deux
  méta-outils ; les 22 schémas amont (59 Ko) ne sont jamais envoyés, et l'inventaire
  complet reste lisible dans `catalog://servers/{nom}/tools`.

> **Réserve honnête : joignable ≠ dans le contexte.** Les ressources étant à *contrôle
> applicatif*, un client qui ignore les ressources ne verra jamais le guide — le modèle
> n'apprend son existence que par la ligne des `instructions` qui y renvoie. Seul un
> client qui expose une lecture de ressource (ou `ResourcesAsTools`, cf. ci-dessous) rend
> le guide réellement accessible au modèle.


## Tests

```bash
uv run pytest                  # 99 tests unitaires, sans réseau
uv run pytest -m integration   # 8 tests bout-en-bout (ports locaux + sous-processus)
```

Les tests d'intégration démarrent un vrai serveur MCP amont et la passerelle en
sous-processus, puis vérifient : outils proxifiés et préfixés, appel d'outil de bout en
bout, `/health` (états, latences, code 503), ressources du catalogue (résumés, domaines,
inventaires, paramètres), refus borné sous saturation de la concurrence, et contenu des
journaux JSON.

## Structure

```
registry.yaml                  registre des serveurs amont (déclaratif, validé)
guide-ssp.md                   guide de déploiement (démarches types), servi dans catalog://guide
scripts/measure_context.py     mesure de l'empreinte sur le contexte (chiffres ci-dessus)
src/mcpgateway/
  registry.py                  modèles Pydantic + chargement/validation du registre
  server.py                    assemblage de la passerelle + CLI
  upstreams.py                 un proxy par amont, sondes, inventaires, sémaphores
  middleware.py                métriques et plafond de concurrence
  metrics.py                   compteurs et latences (percentiles bornés)
  logging_utils.py             logs JSON structurés (et repli texte)
  health.py                    endpoint /health
  catalog.py                   ressources catalog://… (dont catalog://guide)
  guide.md                     guide générique (mécanique), embarqué dans le paquet
tests/
  test_registry.py test_metrics.py test_middleware.py test_upstreams.py
  test_health.py test_catalog.py test_server.py test_guide.py test_end_to_end.py
  (+ fixtures/demo_upstream.py)
```

## Notes d'implémentation

* Un `ProxyProvider` + `ProxyClient` par amont, monté via `mount(proxy, namespace=nom)` :
  la composition est vivante (les changements côté amont sont visibles) et l'espace de
  noms donne le préfixe attendu par les clients.
* Le cache des listes de composants (`cache_ttl`) évite un aller-retour amont à chaque
  résolution d'outil ; `cache_ttl: 0` le désactive pour un amont dynamique.
* `ProxyMetadataMiddleware` transmet les instructions et `_meta` des amonts sans
  remplacer l'identité de la passerelle.
* Rien n'est secret dans le registre : l'authentification vers les amonts n'est pas
  gérée à ce stade (à traiter avant tout déploiement exposé).
