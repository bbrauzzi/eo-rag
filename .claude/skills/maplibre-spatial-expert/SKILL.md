---
name: maplibre-spatial-expert
description: Guide the development of map interfaces with MapLibre GL and Earth Observation (EO/STAC) data.
use-when: User asks to build a map, manage spatial coordinates, or integrate STAC catalog GeoJSON features.
---

# Skill: MapLibre GL & EO/STAC Integration Expert

## Objective
Guide the development of a responsive map interface that displays geometric footprints (GeoJSON) retrieved from LangGraph `stac_search` tool calls.

## Mandatory Development Rules

### 1. Strict Coordinate Handling (Prevent Lat/Lon Inversion)
*   **MapLibre Standard**: Coordinate arrays must strictly follow the `[Longitude, Latitude]` format (EPSG:4326 / WGS84).
*   **Validation**: Inspect incoming data from the STAC endpoint before passing it to MapLibre. 
*   **Correction**: If you detect a `[Latitude, Longitude]` format, programmatically invert the values before rendering.

### 2. WebGL Resource Lifecycle in MapLibre
To prevent memory leaks or "Source/Layer already exists" runtime errors during state changes:
*   Always remove the dependent layer before removing its underlying source.
*   Implement this cleanup sequence before adding new data:
    ```javascript
    if (map.getLayer('stac-footprints-layer')) map.removeLayer('stac-footprints-layer');
    if (map.getSource('stac-source')) map.removeSource('stac-source');
    ```
*   Ensure the map style is completely loaded (`map.on('style.load', ...)`) before calling `addSource` or `addLayer`.

### 3. STAC Data Integration (Earth Observation)
*   Extract the `geometry` field (GeoJSON Polygon/MultiPolygon) or the `bbox` from each retrieved STAC Item.
*   Combine the array of results into a single GeoJSON `FeatureCollection` to maximize WebGL rendering performance.
*   Inject useful metadata (e.g., `id`, `eo:cloud_cover`, `datetime`, `thumbnail`) directly into the `properties` object of each feature.

### 4. Camera and Viewport Management
*   Calculate the total bounding box of the entire `FeatureCollection` whenever LangGraph returns new STAC footprints.
*   Animate the viewport using `map.fitBounds(bbox, { padding: 50, maxZoom: 12, duration: 1500 })`.

### 5. Frontend Performance Optimization
*   Never recreate the map instance (`new maplibregl.Map()`) on component re-renders. 
*   Use a persistent map reference (`useRef` in React) to initialize the canvas exactly once.
