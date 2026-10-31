# Backlog GitHub — issues, discussions et état de publication

**État au 31 octobre 2026.** Le dépôt local porte un commit **non poussé** :
`d8598b1` — *Add registry, health, metrics, limits and catalog resources* (30 fichiers,
+4 701/−45), `main` en avance de 2 sur `origin/main` (`ad31025`, vérifié par `git ls-remote`) : le commit de code, puis celui qui ajoute ce fichier.

Pourquoi rien n'est encore sur GitHub :

| Voie | Résultat |
| --- | --- |
| `git push` (HTTPS) | aucune identité : ni `credential.helper`, ni `~/.git-credentials`, ni jeton (`GITHUB_TOKEN`/`GH_TOKEN`), pas de `gh`. Clé `~/.ssh/id_ed25519` refusée par GitHub (`Permission denied (publickey)`) |
| serveur MCP `github` | connecté (46 outils) mais **identifiant en lecture seule** : création d'issue → `403 Resource not accessible by personal access token`. `push_files`, `create_or_update_file` et `delete_file` relèvent du même droit `contents: write` |
| discussions | **aucun outil de discussion** dans les 46 du serveur MCP |

Ce fichier rassemble donc ce qui reste à déposer, prêt à copier sans réécriture :

1. les **9 issues** (titre + corps) ;
2. les **2 discussions**.

Dès qu'un identifiant disposant de `issues: write` (et `contents: write` pour le code) est
disponible, les textes ci-dessous peuvent être déposés tels quels.
`uv run python scripts/measure_context.py` reproduit les chiffres cités.

## Comment déposer

**Code.** `git push origin main` — il ne manque qu'un identifiant git (enregistrer
`~/.ssh/id_ed25519.pub` sur le compte GitHub, ou stocker un jeton via
`git config --global credential.helper store`). Deux commits locaux sont en attente :
`d8598b1` (le code) et celui qui ajoute ce fichier.

**Issues (9).** Un titre = une ligne `### N. …` ; le corps = tout ce qui suit, jusqu'au
`###` suivant. À déposer via `issue_write(method=create, owner="Mirlon382",
repo="mcpgateway", title=…, body=…)` — nécessite un identifiant avec `issues: write`.
Libellés : `enhancement` et `documentation` (les libellés par défaut de GitHub ; les
libellés `area:*` proposés à l'origine n'existent pas et ne peuvent pas être créés par le
serveur MCP actuel).

**Discussions (2).** Aucun outil de discussion n'est exposé par le serveur MCP : copier les
deux textes ci-dessous dans l'onglet *Discussions* (catégories **Ideas** et **General**),
ou passer par la mutation GraphQL `createDiscussion` avec un identifiant adéquat.


---

## Issues à déposer

### 1. Plan de compatibilité des noms pour le découpage de `mcpdiffusion`

**Labels :** `enhancement`, `breaking-change`, `area:registry`

**Constat.** Le préfixe du serveur est l'**API publique** de la passerelle :
`mount(proxy, namespace=name)` fait que le nom appelé est `{serveur}_{outil}`. Découper
`mcpdiffusion` en `insee` / `melodi` / `rmes` renomme donc **tous** les outils exposés
(`mcpdiffusion_search_melodi_datasets` → `melodi_search_datasets`), ainsi que les URI de
ressources (le namespace est inséré comme segment de chemin : `data://{item}` →
`data://melodi/{item}`, vérifié sur fastmcp 4.1.4). Toute valeur mise en cache côté client
(schémas, noms d'outils dans un system prompt, signets de ressources) devient invalide.

**Proposition.**
1. Choisir des préfixes qui **portent le mot du domaine** (`insee`, `melodi`, `rmes`) :
   BM25 indexe le nom préfixé, donc un préfixe neutre (`ssp`, `data`) casserait la
   découverte par recherche.
2. Garder les anciens noms vivants une version via `ToolTransform` (alias), ou assumer une
   bascule franche en mettant à jour le guide de déploiement dans le même changement.
3. Nommer les schémas de ressources des trois amonts **différemment** de leur nom de
   serveur, pour éviter `rmes://rmes/concept/{id}`.

**Critères d'acceptation**
- [ ] Table de correspondance nom amont → nom exposé, pour les 12 outils actuels.
- [ ] Décision écrite (alias temporaire *ou* bascule franche) et reprise dans le guide.
- [ ] `catalog://servers/{nom}/tools` montre les nouveaux noms après bascule.

**Références.** `src/mcpgateway/upstreams.py` (`mount(namespace=…)`), README § Empreinte
sur le contexte.

---

### 2. Santé : criticité par serveur, pour qu'un amont dégradé ne fasse pas tomber `/health`

**Labels :** `enhancement`, `area:health`

**Constat.** Le contrat actuel renvoie **503 dès qu'un amont activé est `down`** au-delà de
`health.failure_threshold`. Avec un seul amont par producteur, c'est un bon signal ; avec
trois serveurs INSEE destinés à tomber indépendamment, un incident sur `rmes` suffit à
faire redémarrer une passerelle qui sert correctement `insee` et `melodi` — et une
orchestration (conteneur, load balancer) agit sur ce code 503.

**Proposition.** Ajouter un champ de criticité par serveur (ex. `critical: true|false`,
défaut `true`) et faire dépendre le code HTTP des seuls amonts critiques ; les autres
restent visibles en `degraded` avec leur état par serveur (déjà présent dans
`catalog://servers` et dans `?details=1`).

**Critères d'acceptation**
- [ ] Un amont non critique `down` → `200`, `status: "degraded"`, état par serveur détaillé.
- [ ] Un amont critique `down` → `503` (comportement actuel conservé).
- [ ] Tests : les deux cas, plus le cas « aucun amont critique configuré ».
- [ ] README mis à jour (contrat de `/health`).

---

### 3. Catalogue : exposer les prompts et les ressources des amonts

**Labels :** `enhancement`, `area:catalog`

**Constat.** L'inventaire du catalogue est **uniquement composé d'outils** :
`catalog://servers/{nom}` publie `tool_count` et `catalog://servers/{nom}/tools` la liste
des outils. Rien ne compte ni ne liste les prompts et les ressources. C'est cohérent
aujourd'hui (les amonts actuels n'en exposent aucun : `prompts/list` sur `geodata` répond
`Method not found`), mais si les serveurs issus du découpage en publient, ils seront
**invisibles** dans le catalogue — y compris dans `catalog://servers` où l'on cherche
justement à savoir qui expose quoi.

**Proposition.** Étendre `UpstreamStatus` et les ressources du catalogue avec
`prompt_count` / `resource_count` (et, comme pour les outils, un inventaire détaillé
optionnel derrière `catalog.toole`-like switch, par exemple
`catalog://servers/{nom}/prompts` et `/resources`).

**Critères d'acceptation**
- [ ] Compteurs visibles dans `catalog://servers` (0 quand l'amont ne les implémente pas,
      sans erreur).
- [ ] Un amont qui répond `Method not found` reste `ok`, pas `down`.
- [ ] Tests unitaires avec des doubles + test d'intégration sur le serveur de démo.

---

### 4. Guide : préparer le retrait du serveur géographique

**Labels :** `documentation`, `area:guide`

**Constat.** Le guide de déploiement (`guide-ssp.md`) s'appuie sur le serveur géo pour ses
démarches 1 (portrait d'un territoire : `geodata_geocode` → coordonnées → cadastre /
urbanisme / altitude) et 4 (WFS Géoplateforme). Son retrait rend ces deux démarches
injouables ; la démarche 1 est la plus vendeuse du guide.

**Proposition.**
1. Réécrire la démarche 1 sans géocodage : les statistiques INSEE attendent un **nom**
   (`geo_keyword` + `geo_level`), pas des coordonnées — l'étape de géocodage servait à
   canoniser le libellé et à alimenter les outils géo.
2. Supprimer la démarche 4, ou la déplacer dans un guide de déploiement distinct si le
   service géo doit rester documenté ailleurs.
3. Vérifier que le garde-fou de dérive fait bien son travail au moment du retrait : avec un
   registre `insee`/`melodi`/`rmes` et le géo retiré, les **16** noms cités par le guide
   actuel sont signalés comme injoignables (comportement attendu, test
   `test_the_guard_fires_when_a_documented_server_is_removed`).

**Critères d'acceptation**
- [ ] Aucune référence au serveur géo dans `guide-ssp.md` après retrait.
- [ ] `uv run pytest tests/test_guide.py` vert, y compris le test d'intégration qui vérifie
      que chaque outil cité existe encore en amont.
- [ ] Démarche 1 exécutée de bout en bout après réécriture (preuve d'appel réel).

---

### 5. Orchestration inter-serveurs : porter la composition dans la passerelle

**Labels :** `enhancement`, `area:architecture`

**Constat.** Le découpage coupe des enchaînements qui peuvent aujourd'hui être composés «
en process » : les vocabulaires RMES expliquent des variables utilisées dans les jeux de
données Melodi et dans les publications INSEE ; les publications renvoient à des chiffres
Melodi. Après découpage, chaque lien devient un aller-retour HTTP, et **le seul composant
qui détient déjà des clients vers les trois est la passerelle** (ou l'agent, via le guide).

**Proposition.** Choisir explicitement qui orchestre :
(a) l'agent, guidé par `catalog://guide` — état actuel, aucun code ;
(b) un outil authoré par la passerelle (ex. `ssp_analyse_territoriale`) qui appelle les
amonts côté serveur ;
à l'inverse, éviter qu'un amont appelle ses voisins en HTTP (couplage, cycles, ordre de
démarrage porteur).

**Critères d'acceptation**
- [ ] Décision écrite (a ou b) et, si (b), au moins un outil d'orchestration avec test
      d'intégration et gestion de l'indisponibilité partielle d'un amont.
- [ ] Le guide décrit l'enchaînement correspondant.

---

### 6. Outils transverses : décider d'un propriétaire

**Labels :** `enhancement`, `area:registry`

**Constat.** `mcpdiffusion_send_feedback` et `mcpdiffusion_get_insee_homepage` ne sont pas
des outils de domaine. Dupliqués dans les trois serveurs, ils produisent une ambiguïté dans
la recherche BM25 (trois résultats pour « feedback ») ; rattachés à un seul, ils
apparaissent liés à un domaine qui n'est pas le leur.

**Proposition.** Trancher : hébergement par la passerelle (outil unique et stable, routé
vers le bon amont) ou propriétaire désigné parmi les trois.

**Critères d'acceptation**
- [ ] Décision écrite, outil(s) exposé(s) une seule fois.
- [ ] `search_tools("feedback")` renvoie un seul résultat.

---

### 7. Authentification vers les amonts : à faire **après** le découpage

**Labels :** `enhancement`, `area:auth`

**Constat.** `registry.yaml` ne contient **aucun secret** : l'authentification vers les
amonts n'est pas implémentée (choix explicite — un registre versionné ne porte pas
d'identifiants). Le découpage transforme un jeu d'identifiants en trois.

**Proposition.** Séquencer ce chantier **après** le découpage, pour ne configurer qu'une
fois ; prévoir dans le registre un renvoi vers une source de secrets (variable
d'environnement ou fichier hors dépôt) plutôt qu'un champ en clair, et un test qui échoue
si un secret apparaît dans `registry.yaml`.

**Critères d'acceptation**
- [ ] Aucun secret dans `registry.yaml` ; test dédié.
- [ ] Un amont authentifié fonctionne de bout en bout (outil appelé, inventaire lu).
- [ ] Erreur lisible quand un identifiant manque ou est refusé (pas de fuite dans le
      message d'erreur renvoyé au client).

---

### 8. Charge de connexion avec trois amonts : mesurer avant/après

**Labels :** `performance`, `area:metrics`

**Constat.** Le coût d'agrégation augmente avec le nombre d'amonts, pas la charge envoyée
au modèle : chaque amont est sondé et son inventaire lu à la connexion (`mcpdiffusion`
206–346 ms, `geodata` 813–968 ms — en parallèle, donc P99 ≈ l'amont le plus lent, borné par
`timeout`). Avec trois serveurs, P99 dépend plus souvent d'un service tiers, et le plafond
de concurrence global (16) est partagé entre trois amonts.

**Proposition.** Passer `scripts/measure_context.py` sur le registre cible à trois amonts,
relever les latences d'inventaire par amont, et régler `cache_ttl` (300 s aujourd'hui) et
`concurrency.global_limit` en conséquence ; envisager un préchauffage des inventaires au
démarrage.

**Critères d'acceptation**
- [ ] Mesures avant/après avec trois amonts, reportées dans le README.
- [ ] Décision écrite sur `cache_ttl` et `global_limit`.

---

### 9. README : recadrer la proposition de valeur après le retrait du géo

**Labels :** `documentation`

**Constat.** Le README présente la passerelle comme une **fédération multi-producteurs**
(« agrège les serveurs MCP des producteurs de statistiques publiques »), avec des mesures
sur deux amonts de deux producteurs. Avec un seul producteur et trois serveurs spécialisés,
l'argument change de nature : un point d'entrée unique, une discipline de nommage, des
limites et une santé centralisées, et surtout l'**économie de contexte** (3,7 Ko au lieu de
60,1 Ko, mesuré).

**Proposition.** Réécrire l'introduction et l'exemple de registre pour refléter le
périmètre cible, sans perdre le chiffre de contexte, qui reste l'argument le plus fort.

**Critères d'acceptation**
- [ ] Introduction, exemple de registre et tableau de mesures cohérents avec le périmètre
      réel.
- [ ] Chiffres reproductibles via `uv run python scripts/measure_context.py`.
---

## Discussions à déposer

*(onglet Discussions du dépôt ;
catégories proposées : **Ideas** pour la 1, **General** pour la 2)*

### Discussion 1 — Découper `mcpdiffusion` en trois serveurs spécialisés (`insee`, `melodi`, `rmes`) : ce que ça casse

**Catégorie :** Ideas

**Contexte.** `mcpdiffusion` est aujourd'hui un serveur unique exposant 12 outils couvrant
quatre usages distincts (publications et chiffres-clés INSEE, jeux de données Melodi,
référentiels RMES, conjoncture, plus deux outils transverses). L'intention est de le
découper en trois serveurs spécialisés — `insee.fr`, `melodi`, `rmes` — servis derrière la
passerelle, et de retirer par ailleurs le serveur géographique.

Ce qui suit est la liste des points de vigilance identifiés en relisant la passerelle et le
protocole, avec ce qui a été **vérifié** et ce qui relève du choix d'architecture.

**1. Le préfixe devient l'API publique.** `mount(proxy, namespace=name)` expose
`{serveur}_{outil}` ; un renommage de serveur renomme donc tous ses outils. Conséquence : le
découpage est le bon moment pour renommer (il n'y en aura pas de gratuit ensuite), et les
clients qui mettent en cache noms et schémas cassent à la bascule. BM25 indexant le nom
préfixé, les préfixes doivent porter le mot du domaine — `insee`, `melodi`, `rmes` : oui ;
`ssp` ou `data` : non.

**2. Les URI de ressources suivent le même sort.** Vérifié sur fastmcp 4.1.4 : le namespace
est inséré comme **segment de chemin** après le schéma (`data://{item}` → `data://melodi/{item}`,
`config://static` → `config://insee/static`). Deux amonts qui exposeraient le même schéma
sont donc désambiguïsés automatiquement — la collision n'est pas un risque. En revanche le
nom du serveur entre dans l'URI : éviter le bégaiement (`rmes://rmes/concept/{id}`) en
choisissant des schémas différents des noms de serveur. Les prompts, eux, sont exposés
`{serveur}_{prompt}` (`insee_resume`, vérifié).

**3. Le découpage coupe des coutures qui font le produit.** Les liens réellement
inter-serveurs : les vocabulaires RMES expliquent des variables utilisées dans les jeux
Melodi et les publications INSEE ; les publications renvoient à des chiffres Melodi.
Aujourd'hui composables en process, ils deviennent des allers-retours HTTP. Seuls deux
acteurs peuvent les recomposer sans dupliquer la logique : **l'agent** (via le guide) ou un
**outil authoré par la passerelle**, qui détient déjà des clients vers les trois. À l'inverse,
faire appeler un amont par ses voisins en HTTP crée du couplage, des cycles et un ordre de
démarrage porteur.

**4. Le retrait du géo emporte la démarche la plus vendeuse du guide.** Les démarches 1 et 4
de `guide-ssp.md` reposent sur `geodata_geocode` → coordonnées. Les statistiques INSEE
attendent un **nom** (`geo_keyword` + `geo_level`), donc la démarche 1 survit sans
géocodage, mais perd les outils de contexte géographique (cadastre, urbanisme, altitude).
À réécrire dans le même changement.

**5. `/health` devient plus fragile à trois amonts.** Le contrat actuel renvoie `503` dès
qu'un amont activé est `down`. Un incident sur `rmes` suffirait à faire redémarrer une
passerelle qui sert correctement les deux autres. Proposition : une criticité par serveur,
les amonts non critiques restant visibles en `degraded` (200) avec leur état détaillé.

**6. Le catalogue ne voit que les outils.** `catalog://servers/{nom}` publie `tool_count` ;
prompts et ressources ne sont ni comptés ni listés. Si les trois nouveaux serveurs en
exposent, ils seront invisibles dans le catalogue jusqu'à extension. À noter aussi : les
prompts amont arriveront dans le sélecteur utilisateur à côté de ceux de la passerelle.

**7. La charge envoyée au modèle ne bouge pas.** Le coût d'agrégation, si : trois sondes et
trois inventaires à la connexion (mesuré aujourd'hui : 206–346 ms pour `mcpdiffusion`,
813–968 ms pour `geodata`, en parallèle, donc P99 ≈ le plus lent). Le plafond de concurrence
global (16) est partagé entre trois amonts. La charge de connexion, elle, reste à ~3,7 Ko :
c'est précisément le rôle de la recherche d'outils et du catalogue en ressources.

**8. Outils transverses.** `send_feedback` et `get_insee_homepage` ne sont pas des outils de
domaine : dupliqués trois fois, ils créent une ambiguïté de recherche ; rattachés à un seul,
ils semblent appartenir à un domaine qui n'est pas le leur. Décider d'un propriétaire (ou
les héberger dans la passerelle).

**9. Authentification et bascule.** `registry.yaml` ne porte aucun secret (choix explicite).
Trois amonts = trois jeux d'identifiants : à faire **après** le découpage, pas deux fois.
Pour la bascule, on peut enregistrer les quatre serveurs d'emblée (`enabled: false` sur les
nouveaux) puis basculer, avec des alias `ToolTransform` si l'on veut garder les anciens noms
une version.

**Question ouverte.** Qui orchestre les enchaînements inter-serveurs : l'agent guidé par le
guide, ou un outil authoré par la passerelle ? Et sous quel nom les outils des trois
nouveaux serveurs seront-ils exposés ?

---

### Discussion 2 — Quelle surface de prompts et de skills la passerelle doit-elle exposer ?

**Catégorie :** General

**Contexte.** La passerelle agrège des amonts aujourd'hui **purement « outils »** :
`mcpdiffusion` expose 12 outils, 0 prompt, 0 ressource ; `geodata` répond `Method not found`
à `prompts/list` et `resources/list`. La question posée était : jusqu'où ajouter des prompts,
et faut-il « exposer un skill » ?

**Ce qui gouverne la réponse, côté protocole.** Les trois primitives n'ont pas le même
destinataire : les **outils** sont à contrôle du modèle (leurs schémas partent dans le
contexte), les **prompts** à contrôle de l'utilisateur (ils apparaissent dans l'interface,
pas dans le contexte du modèle), les **ressources** à contrôle applicatif (le client décide,
et leur contenu ne part que sur `resources/read`). Corollaire : tout ce que le modèle doit
savoir va dans `instructions` ou dans une ressource, **jamais** dans un prompt.

**Prompts : oui, mais peu, et authorés par la passerelle.** Cible d'environ quatre prompts,
dont un généré depuis le registre (`decouvrir_la_passerelle` : serveurs, domaines, règle de
nommage, catalogue masqué, URI `catalog://`), plus des enchaînements qui traversent les
amonts (portrait d'un territoire, note de synthèse, données à la carte). Rejetés : un prompt
par outil (le sélecteur devient illisible et pourrit à chaque renommage), la copie des
prompts amonts, et l'idée de mettre une connaissance critique dans un prompt. `PromptsAsTools`
permet de les convertir en outils pour les clients qui n'ont pas de surface de prompts.

**Skills : oui pour le contenu, non pour l'API.** La version publiée de FastMCP (4.1.4) sert
bien `SkillsDirectoryProvider` (`skill://<nom>/SKILL.md`), mais l'extension client
`SkillsClientExtension` n'existe pas : `skills/list` est inatteignable, aucun client ne peut
s'y abonner. La documentation décrit l'API **4.2, non publiée**. Conclusion : livrer le
contenu comme **une seule ressource markdown** (`catalog://guide`, avec son frontmatter
`name`/`description`), et l'envelopper plus tard dans le fournisseur de skills sans
réécrire une ligne.

**Ce qu'est le guide, en deux couches.** La **mécanique** est fournie par le paquet
(`src/mcpgateway/guide.md`) : nommage, catalogue masqué derrière `search_tools`/`call_tool`,
ressources du catalogue, pièges — sans citer aucun serveur ni outil du déploiement, donc
valable pour tout registre. Les **démarches** viennent du déploiement (`guide-ssp.md`, via
`gateway.catalog.guide_path`), ajoutées après un séparateur. Le guide est du récit et des
renvois, **jamais un inventaire** : l'inventaire est généré (`instructions`,
`catalog://servers*`), sinon c'est une seconde source de vérité qui périme.

**Réserve à garder en tête : joignable ≠ dans le contexte.** Les ressources étant à contrôle
applicatif, un client qui ignore les ressources ne verra jamais le guide ; le modèle n'en
apprend l'existence que par la ligne des `instructions` qui y renvoie. `ResourcesAsTools`
rend le guide *appelable* par le modèle, sans garantir qu'il le lise.

**Chiffres mesurés** (`uv run python scripts/measure_context.py`, registre à 22 outils) :
connexion = 3,7 Ko avec la recherche d'outils, 60,1 Ko sans (×16,1) ; `resources/list` =
917 o d'en-têtes seuls ; `catalog://guide` = 8 302 caractères, transférés uniquement à la
lecture.

**Question ouverte.** Construit-on les quatre prompts maintenant, ou seulement le prompt de
découverte généré et le guide ? Et faut-il activer `ResourcesAsTools` pour les clients qui
ignorent les ressources ?
