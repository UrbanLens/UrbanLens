<script>
  import { onMount } from 'svelte';

  let map;
  let center = [40.730610, -73.935242]; // New York coordinates

  onMount(async () => {
    mapboxgl.accessToken = import.meta.env.VITE_MAPBOX_ACCESS_TOKEN;
    map = new mapboxgl.Map({
      container: 'map',
      style: 'mapbox://styles/mapbox/streets-v11',
      center: center,
      zoom: 12
    });

    new mapboxgl.Marker()
      .setLngLat(center)
      .addTo(map);
  });
</script>

<div class="section no-pad-bot" id="index-banner">
  <div class="container">
    <h1 class="header center orange-text">Map</h1>
  </div>
</div>

<div class="container">
  <div id="map" style="height: 500px;"></div>
</div>
