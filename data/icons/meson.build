application_id = 'org.tsutsen.SwitchROMTools'

# Install application icons
scalable_dir = 'hicolor' / 'scalable' / 'apps'
install_data(
  scalable_dir / ('@0@.svg').format(application_id),
  install_dir: get_option('datadir') / 'icons' / scalable_dir
)

symbolic_dir = 'hicolor' / 'symbolic' / 'apps'
install_data(
  symbolic_dir / ('@0@-symbolic.svg').format(application_id),
  install_dir: get_option('datadir') / 'icons' / symbolic_dir
)

# Install action icons
actions_dir = 'hicolor' / 'scalable' / 'actions'
install_data(
  actions_dir / 'decompress-icon-symbolic.svg',
  install_dir: get_option('datadir') / 'icons' / actions_dir
)

install_data(
  actions_dir / 'compress-icon-symbolic.svg',
  install_dir: get_option('datadir') / 'icons' / actions_dir
)