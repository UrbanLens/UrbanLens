<style>
  .map {
		position: absolute;
		width: 100%;
		height: 100%;
	}
  .sidebar {
    background-color: rgba(35, 55, 75, 0.9);
    color: #fff;
    padding: 6px 12px;
    font-family: monospace;
    z-index: 1;
    position: absolute;
    top: 0;
    left: 0;
    margin: 12px;
    border-radius: 4px;
  }
</style>

<script>
  import { onMount, onDestroy } from 'svelte';
  import mapbox from 'mapbox-gl';
	import "../../../node_modules/mapbox-gl/dist/mapbox-gl.css"

  /**
   * @type {mapbox.Map}
   */
  let map;
  /**
   * @type {HTMLDivElement}
   */
  let mapContainer;
  let lat = 42.65698624597273;
  let lng = -73.75144231302086;
  let zoom = 9;

  function updateData() {
    	zoom = map.getZoom();
    	lng = map.getCenter().lng;
    	lat = map.getCenter().lat;
  }

	onMount(async () => {
		const initialState = { lng: lng, lat: lat, zoom: zoom };

		map = new mapbox.Map({
			container: mapContainer,
			accessToken: import.meta.env.VITE_MAPBOX_API_TOKEN,
			style: `mapbox://styles/mapbox/outdoors-v11`,
			center: [initialState.lng, initialState.lat],
			zoom: initialState.zoom,
		});

		map.on('move', () => {
			updateData();
		})

		const response = await fetch('/api/locations', {
			method: 'GET',
			headers: {
				'Content-Type': 'application/json'
			}
		});

		if (response.ok) {
			const locations = await response.json();
			locations.forEach(location => {
				new mapbox.Marker()
					.setLngLat([location.lng, location.lat])
					.addTo(map);
			});
		} else {
			console.error('Failed to load locations');
		}
	});


	onDestroy(() => {
		if (map) {
      map.remove();
    }
	});
</script>

<div>
  <div class="sidebar">
    Longitude: {lng.toFixed(4)} | Latitude: {lat.toFixed(4)} | Zoom: {zoom.toFixed(2)}
  </div>
  <div class="map-wrap">
    <div class="map" bind:this={mapContainer} on:contextmenu|preventDefault={showContextMenu} />
    {#if contextMenuVisible}
      <div class="context-menu" style="top: {contextMenuY}px; left: {contextMenuX}px;">
        <ul>
          <li on:click={addLocation}>Add a location</li>
        </ul>
      </div>
    {/if}
  </div>
</div>
