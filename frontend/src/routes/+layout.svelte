<script>
  import Footer from '../components/Footer.svelte';
  import Header from '../components/Header.svelte';
</script>

<Header />

<main style="padding: 20px;">
  <slot />
</main>

<Footer />
