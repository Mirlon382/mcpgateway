---
name: passerelle-mcp
description: Utiliser une passerelle MCP agrégée — nommage des outils, découverte du catalogue masqué, ressources de catalogue et pièges courants.
---

# Utiliser cette passerelle MCP

Cette passerelle agrège plusieurs serveurs MCP derrière un point d'entrée unique. Elle ne
réimplémente pas les outils des serveurs amont : elle les proxifie, les renomme et les
rend découvrables.

## 1. Tous les composants sont préfixés

| Composant | Forme exposée |
| --- | --- |
| Outil | `{serveur}_{outil}` (un serveur `meteo` exposant `get_forecast` devient `meteo_get_forecast`) |
| Prompt | `{serveur}_{prompt}` |
| Ressource | `scheme://{serveur}/chemin` |

Le préfixe identifie **la source à interroger** : il se lit dans le nom de l'outil. La
liste des serveurs et de leurs domaines est dans `catalog://servers`.

## 2. Le catalogue d'outils peut être masqué

Si la recherche d'outils est activée, `tools/list` ne renvoie **pas** les outils amont
mais deux outils de découverte :

* `search_tools(query)` — cherche dans les noms, descriptions et paramètres des outils ;
* `call_tool(name, arguments)` — exécute l'outil découvert.

Séquence normale :

1. lire `catalog://servers` (et `catalog://domains`) pour situer le bon serveur ;
2. appeler `search_tools` avec une requête **décrivant la capacité** (« prix à la
   consommation », « géocodage d'une adresse ») ;
3. appeler `call_tool` avec le nom préfixé renvoyé par la recherche.

## 3. Lire l'inventaire et l'état

| Ressource | Contenu |
| --- | --- |
| `catalog://servers` | serveurs : domaine, description, tags, `enabled`, préfixe, limites, état de joignabilité |
| `catalog://domains` | serveurs regroupés par domaine fonctionnel |
| `catalog://servers/{nom}` | détail d'un serveur + ses métriques (latences, erreurs) |
| `catalog://servers/{nom}/tools` | inventaire : nom amont, **nom à appeler**, description, paramètres |
| `catalog://guide` | ce document |

Ces ressources sont la source de vérité pour ce qui existe : préférez-les à une liste
recopiée dans un prompt ou un guide.

## 4. Pièges connus

* **La recherche porte sur le texte des outils**, pas sur le vocabulaire du registre :
  une requête comme « diffusion » peut ne rien renvoyer alors que le domaine existe.
  Passez par `catalog://domains` pour la découverte par domaine.
* **Tous les serveurs amont n'implémentent pas toutes les capacités.** Un serveur
  uniquement « outils » répond `Method not found` à `prompts/list` ou `resources/list`.
  La passerelle ignore ce serveur pour ces méthodes au lieu d'échouer : l'erreur ne
  doit pas être attribuée à la passerelle.
* **`Gateway is busy: …` est une limite de concurrence**, pas une panne : l'appel a
  attendu un créneau et a été refusé pour ne pas saturer l'amont. Réessayez un peu plus
  tard, ou répartissez la charge.
* **Chaque amont a son propre délai.** Un outil lourd peut expirer côté passerelle alors
  que l'amont répond encore : réduisez la requête (filtres, nombre de résultats).
* **Un serveur désactivé reste visible** dans `catalog://servers` (`enabled: false`,
  `status.state: "disabled"`) mais n'expose aucun composant.
* `status: "unavailable"` dans un inventaire signifie que l'amont n'a pas répondu ; le
  catalogue n'échoue jamais pour autant.

## 5. Démarches propres à ce déploiement

Les démarches types et les workflows multi-serveurs de cette installation sont ajoutés à
la suite de ce document lorsqu'un guide de déploiement est configuré
(`gateway.catalog.guide_path`). S'il n'y en a pas, considérez la section 4 et les
ressources du catalogue comme point de départ.
