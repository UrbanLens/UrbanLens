<script>
    import { onMount, onDestroy } from 'svelte';
    import mapbox from 'mapbox-gl';
    import "../../../node_modules/mapbox-gl/dist/mapbox-gl.css";
    import { DEFAULT_LATITUDE, DEFAULT_LONGITUDE } from './utilities';
  
    /**
     * @type {mapbox.LngLatLike}
     */
    export let userCoordinates;

    /**
     * @type {mapbox.Map}
     */
    let map;
    /**
     * @type {HTMLDivElement}
     */
    let mapContainer;
    /**
     * @type {number}
     */
    let lat = DEFAULT_LATITUDE;
    /**
     * @type {number}
     */
    let lng = DEFAULT_LONGITUDE;
    /**
     * @type {mapbox.Zoom}
     */
    let zoom = 9;
  
    function updateData() {
      zoom = map.getZoom() || zoom;
      lng = map.getCenter().lng || lng;
      lat = map.getCenter().lat || lat;
    }

    $: if (userCoordinates) {
        lat = userCoordinates.lat;
        lng = userCoordinates.lng;
        const initialState = { lng, lat, zoom };

        map = new mapbox.Map({
            container: mapContainer,
            accessToken: import.meta.env.VITE_MAPBOX_API_TOKEN,
            style: 'mapbox://styles/mapbox/outdoors-v11',
            center: [initialState.lng, initialState.lat],
            zoom: initialState.zoom,
        });

        map.on('move', updateData);
    }

    onDestroy(() => {
        map && map.remove();
    });
</script>
<style>
    .map-container {
        position: relative;
        height: calc(100vh - 60px); 
        width: 100%;
    }
    .map {
      position: absolute;
      top: 0;
      bottom: 0;
      width: 100%;
      height: 100%;
    }
    .sidebar {
        background-color: rgba(35, 55, 75, 0.7); 
        color: #fff;
        padding: 6px 12px;
        font-family: monospace;
        z-index: 1;
        position: absolute;
        top: 10px; 
        left: 10px;
        border-radius: 4px;
    }
</style>
  
<div class="map-container">
    <div class="sidebar">
        Longitude: {lng.toFixed(4)} | Latitude: {lat.toFixed(4)} | Zoom: {zoom.toFixed(2)}
    </div>
	<div class="map-wrap">
        <div class="map" bind:this={mapContainer} />
	</div>
</div>