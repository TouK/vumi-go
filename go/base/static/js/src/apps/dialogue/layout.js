(function(exports) {
  var Grid = go.components.grid.Grid;


  var DialogueStateLayout = Backbone.View.extend({
    initialize: function(options) {
      options = _.defaults(options, {numCols: 3});

      this.states = options.states;
      this.numCols = options.numCols;
      this.colWidth = options.colWidth;
      this.setElement(this.states.view.$el);
      this.grid = new Grid({numCols: this.numCols});
      this.initDragging();
    },

    render: function() {
      this.states.each(this.renderState, this);
    },

    offsetOf: function(coords) {
      var offset = this.$el.offset();

      return {
        left: offset.left + coords.x,
        top: offset.top + coords.y
      };
    },

    repaint: function() {
      // TODO only repaint the relevant connections
      jsPlumb.repaintEverything();
      this.trigger('repaint');
    },

    resizeToFit: function(state) {
      var height = this.$el.height();
      var y = state.$el.position().top + state.$el.outerHeight(true);
      this.$el.height(Math.max(height, y));
    },

    renderState: function(state) {
      var layout = this.ensureLayout(state);
      state.$el.offset(this.offsetOf(layout.coords()));
      this.resizeToFit(state);
    },

    ensureLayout: function(state) {
      if (!state.model.has('layout')) this.initLayout(state);
      return state.model.get('layout');
    },

    initLayout: function(state) {
      var marginLeft = parseInt(state.$el.css('marginLeft'));
      var marginTop = parseInt(state.$el.css('marginTop'));

      var cell = this.grid.add({
        width: state.$el.outerWidth(true) - marginLeft,
        height: state.$el.outerHeight(true) - marginTop
      });

      state.model.set('layout', {
        x: cell.x + marginLeft,
        y: cell.y + marginTop
      });
    },

    initDragging: function() {
      var onDrag = this.onDrag.bind(this);

      this.states.each(function(state) {
        state.$el.draggable({
          drag: onDrag,
          handle: '.titlebar'
        });
      }, this);
    },

    onDrag: function(e) {
      var state = this.states.get($(e.target).attr('data-uuid'));

      state.model
        .get('layout')
        .set(offsetCoords(state.$el.position()), {silent: true});

      this.repaint();
      this.resizeToFit(state);
    }
  });


  function offsetCoords(offset) {
    return {
      x: offset.left,
      y: offset.top
    };
  }


  exports.DialogueStateLayout = DialogueStateLayout;
})(go.apps.dialogue.layout = {});
