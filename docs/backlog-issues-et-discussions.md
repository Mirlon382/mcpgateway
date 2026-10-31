# Backlog GitHub — issues, discussions et état de publication

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
