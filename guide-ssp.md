# Démarches types — passerelle SSP (INSEE + IGN)

Ceci est le guide de déploiement de la passerelle du Service Statistique Public. Il
complète le guide générique servi avant lui dans `catalog://guide` : il décrit ce que
cette installation expose et comment enchaîner les serveurs.

> Les noms d'outils cités sont les noms **à appeler** (préfixés). L'inventaire fait foi :
> `catalog://servers/mcpdiffusion/tools` et `catalog://servers/geodata/tools`.

## Les deux serveurs

| Serveur | Domaine | Ce qu'il sert |
| --- | --- | --- |
| `mcpdiffusion` | `diffusion` | Publications et chiffres de l'INSEE : chiffres-clés, publications (Insee Première, Analyses…), jeux de données Melodi, base de métadonnées RMES, conjoncture |
| `geodata` | `geographie` | Données géographiques de l'IGN : géocodage, découpage administratif, cadastre, urbanisme, altitude, WFS Géoplateforme |

**`geodata` n'expose que des outils** : il n'implémente ni `prompts/list` ni
`resources/list` (réponse `Method not found`). C'est une limite du serveur amont, pas de
la passerelle, qui continue de fonctionner normalement.

## Démarche 1 — Portrait d'un territoire (le cas le plus demandé)

Le point clé : `geodata` travaille en **coordonnées** (`lon`/`lat`), les statistiques
INSEE en **nom de territoire** (`geo_keyword` + `geo_level`). Le géocodage est donc la
porte d'entrée des deux :

1. `geodata_geocode(text="Beauvais")` → renvoie le libellé complet, `kind`, `city`,
   `zipcode` et des coordonnées `lon`/`lat` réutilisables partout ailleurs.
2. `mcpdiffusion_search_insee_chiffrecle(query="population, logement, emploi",
   geo_level="COM", geo_keyword="Beauvais")` → tableaux synthétiques prêts à l'emploi.
   `geo_level` accepte `COM`, `DEP`, `REG`, `INTER`, `COMPRD`, `FRANCE`.
3. Contexte géographique au même point : `geodata_adminexpress(lon, lat)` (unités
   administratives), `geodata_cadastre(lon, lat)`, `geodata_urbanisme(lon, lat)`,
   `geodata_altitude(lon, lat)`.

Toutes les réponses sont des données ; c'est vous qui composez la synthèse.

## Démarche 2 — Chercher une publication puis la lire

1. `mcpdiffusion_search_insee_documents(query="effets du covid sur le tourisme",
   geo_level="REG", geo_keyword="Occitanie", year_of_reference=2024)` — la requête est
   en langage naturel : mettez-y des synonymes et le contexte (année, géographie).
2. `mcpdiffusion_get_insee_document(document_urls=["<url du résultat>"])` → texte complet.
3. Pour un chiffre ponctuel plutôt qu'une analyse rédigée, préférez
   `mcpdiffusion_search_insee_chiffrecle`. Pour un indicateur mensuel récent :
   `mcpdiffusion_search_insee_conjoncture`.

## Démarche 3 — Série statistique fine (Melodi)

Melodi se parcourt en trois temps, dans cet ordre :

1. `mcpdiffusion_search_melodi_datasets(query="indice des prix a la consommation",
   start_year=2015)` → `dataset_id` et colonnes disponibles. La recherche est lexicale :
   écrivez la requête en français explicite (« décès par département », « prénoms des
   nouveau-nés »).
2. `mcpdiffusion_search_melodi_modalities(dataset_id=…, column_ids=["GEO"], query="…")`
   → codes de modalité valides. Cette étape évite des filtres inventés.
3. `mcpdiffusion_get_melodi_observations(dataset_id=…, years=[2020, 2021],
   column_filters={"GEO": "<code modalité>"}, number_of_observations=…)` → observations
   avec dimensions, unités et mesure.

Pour le vocabulaire, les définitions et les nomenclatures (et **non** les chiffres) :
`mcpdiffusion_search_rmes_graphs` puis `mcpdiffusion_run_rmes_sparql`, puis
`mcpdiffusion_describe_rmes_resource` pour le détail d'une ressource.

## Démarche 4 — Données WFS de la Géoplateforme

1. `geodata_gpf_wfs_search_types(query="…")` → types disponibles.
2. `geodata_gpf_wfs_describe_type(typename=…)` → schéma (identifiants et attributs).
3. `geodata_gpf_wfs_get_features(typename=…, where="…", bbox_west=…, bbox_south=…,
   bbox_east=…, bbox_north=…)` ou `geodata_gpf_wfs_get_feature_by_id(typename=…,
   feature_id=…)`.

Pas besoin d'écrire du CQL : les filtres attributaires et spatiaux sont des paramètres.

## Rappels opérationnels

* Découverte : `catalog://servers`, `catalog://domains`, puis
  `catalog://servers/{nom}/tools`. La recherche d'outils (si activée) porte sur le texte
  des outils — « melodi », « cadastre », « insee » fonctionnent, un mot de domaine comme
  « diffusion » non.
* Un amont peut être temporairement indisponible : `catalog://servers` porte l'état et la
  dernière erreur, `/health` (HTTP) donne l'état détaillé et les métriques.
* `mcpdiffusion_send_feedback(author, feedback)` existe côté amont pour signaler un
  problème de contenu INSEE — ce n'est pas un canal de support pour la passerelle.
