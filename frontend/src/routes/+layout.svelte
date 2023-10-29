<script>
  import Footer from '../components/Footer.svelte';
  import Header from '../components/Header.svelte';
</script>

<Header />

<nav>
  <a href="/login">Login</a>
  <a href="/logout">Logout</a>
</nav>

<main style="padding: 20px;">
  <slot />
</main>

<Footer />
