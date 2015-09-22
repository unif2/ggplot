from __future__ import (absolute_import, division, print_function,
                        unicode_literals)
import sys
from copy import deepcopy

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.offsetbox import AnchoredOffsetbox
from six.moves import zip

from .components.aes import make_labels
from .components.panel import Panel
from .components.layer import add_group
from .facets import facet_null, facet_grid, facet_wrap
from .themes.theme_gray import theme_gray
from .utils import is_waive, suppress
from .utils.exceptions import GgplotError
from .utils.ggutils import gg_context
from .scales.scales import Scales
from .scales.scales import scales_add_missing
from .coords import coord_cartesian
from .guides.guides import guides


# Show plots if in interactive mode
if sys.flags.interactive:
    plt.ion()


class ggplot(object):
    """
    ggplot is the base layer or object that you use to define
    the components of your chart (x and y axis, shapes, colors, etc.).
    You can combine it with layers (or geoms) to make complex graphics
    with minimal effort.

    Parameters
    -----------
    aesthetics :  aes (ggplot.components.aes.aes)
        aesthetics of your plot
    data :  pandas DataFrame (pd.DataFrame)
        a DataFrame with the data you want to plot

    Examples
    ----------
    >>> p = ggplot(aes(x='x', y='y'), data=diamonds)
    >>> print(p + geom_point())
    """

    CONTINUOUS = ['x', 'y', 'size', 'alpha']
    DISCRETE = ['color', 'shape', 'marker', 'alpha', 'linestyle']

    def __init__(self, mapping, data):
        if not isinstance(data, pd.DataFrame):
            mapping, data = data, mapping

        self.data = data
        self.mapping = mapping
        self.facet = facet_null()
        self.labels = make_labels(mapping)
        self.layers = []
        self.guides = guides()
        self.scales = Scales()
        # default theme is theme_gray
        self.theme = theme_gray()
        self.coordinates = coord_cartesian()
        self.plot_env = mapping.aes_env

    def __repr__(self):
        """Print/show the plot"""
        # We're going to default to making the plot appear
        # when __repr__ is called.
        self.draw()
        plt.show()
        # TODO: We can probably get more sugary with this
        return "<ggplot: (%d)>" % self.__hash__()

    def __deepcopy__(self, memo):
        """deepcopy support for ggplot"""
        # This is a workaround as ggplot(None, None)
        # does not really work :-(
        class _empty(object):
            pass
        result = _empty()
        result.__class__ = self.__class__
        # don't make a deepcopy of data, or plot_env
        shallow = {'data', 'plot_env'}
        for key, item in self.__dict__.items():
            if key in shallow:
                result.__dict__[key] = self.__dict__[key]
                continue
            result.__dict__[key] = deepcopy(self.__dict__[key], memo)

        return result

    def draw(self):
        """
        Render the complete plot and return the matplotlib figure
        """
        plt.close("all")  # TODO: Remove before merging into mainline
        with gg_context(theme=self.theme):
            plot = self.draw_plot()
            plot = self.draw_legend(plot)
            # Theming
            for ax in plot.axs:
                plot.theme.apply(ax)

        return plot.figure

    def draw_plot(self):
        """
        Draw the main plot(s) onto the axes.

        Return
        ------
        out : ggplot
            ggplot object with two new properties
                - axs
                - figure
        """
        data, panel, plot = self.build()
        figure, axs = plt.subplots(plot.facet.nrow,
                                   plot.facet.ncol,
                                   sharex=False,
                                   sharey=False)

        figure._themeable = {}
        axs = np.atleast_2d(axs)
        axs = [ax for row in axs for ax in row]
        for ax in axs[len(panel.layout):]:
            ax.axis('off')
            ax._themeable = {}
        axs = axs[:len(panel.layout)]
        plot.axs = axs
        plot.figure = figure
        plot.theme.figure = figure

        # ax - axes for a particular panel
        # finfo - panel (facet) information from layout table
        for ax, (_, finfo) in zip(axs, panel.layout.iterrows()):
            panel_idx = finfo['PANEL'] - 1
            scales = panel.ranges[panel_idx]

            # Plot all data for each layer
            for zorder, (l, d) in enumerate(
                    zip(plot.layers, data), start=1):
                bool_idx = (d['PANEL'] == finfo['PANEL'])
                l.draw(d[bool_idx], scales, plot.coordinates,
                       ax, zorder)

            # xaxis & yaxis breaks and labels and stuff
            set_breaks_and_labels(plot, panel.ranges[panel_idx],
                                  finfo, ax)
            # draw facet labels
            if isinstance(plot.facet, (facet_grid, facet_wrap)):
                draw_facet_label(plot, finfo, ax)

        apply_facet_spacing(plot)
        add_labels_and_title(plot)

        return plot

    def build(self):
        """
        Build ggplot for rendering.

        This function takes the plot object, and performs all steps
        necessary to produce an object that can be rendered.

        Returns
        -------
        data : list
            dataframes, one for each layer
        panel : panel
            panel object with all the finformation required
            for ploting
        plot : ggplot
            A copy of the ggplot object
        """
        # TODO:
        # - copy the plot_data in here and give each layer
        #   a separate copy. Currently this is happening in
        #   facet.map_layout
        # - Do not alter the user dataframe, create a copy
        #   that keeps only the columns mapped to aesthetics.
        #   Currently, this space conservation is happening
        #   in compute_aesthetics. Can we get this evaled
        #   dataframe before train_layout!!!
        if not self.layers:
            raise GgplotError('No layers in plot')

        plot = deepcopy(self)

        layers = plot.layers
        layer_data = [x.data for x in plot.layers]
        all_data = [plot.data] + layer_data
        scales = plot.scales

        # Initialise panels, add extra data for margins & missing
        # facetting variables, and add on a PANEL variable to data
        panel = Panel()
        panel.layout = plot.facet.train_layout(all_data)
        data = plot.facet.map_layout(panel.layout, layer_data, plot.data)

        # Compute aesthetics to produce data with generalised variable names
        data = [l.compute_aesthetics(d, plot) for l, d in zip(layers, data)]

        # Transform data using all scales
        data = [scales.transform_df(d) for d in data]

        # Map and train positions so that statistics have access
        # to ranges and all positions are numeric
        def scale_x():
            return scales.get_scales('x')

        def scale_y():
            return scales.get_scales('y')

        panel.train_position(data, scale_x(), scale_y())
        data = panel.map_position(data, scale_x(), scale_y())

        # Apply and map statistics
        data = [l.compute_statistic(d, panel)
                for l, d in zip(layers, data)]
        data = [l.map_statistic(d, plot) for l, d in zip(layers, data)]
        # data = [order_groups(d) for d in data)] # !!! look into this

        # Make sure missing (but required) aesthetics are added
        scales_add_missing(plot, ('x', 'y'))

        # Reparameterise geoms from (e.g.) y and width to ymin and ymax
        data = [l.reparameterise(d) for l, d in zip(layers, data)]

        # Apply position adjustments
        data = [l.compute_position(d, panel)
                for l, d in zip(layers, data)]

        # Reset position scales, then re-train and map.  This ensures
        # that facets have control over the range of a plot:
        #   - is it generated from what's displayed, or
        #   - does it include the range of underlying data
        panel.reset_scales()
        panel.train_position(data, scale_x(), scale_y())
        data = panel.map_position(data, scale_x(), scale_y())

        # Train and map non-position scales
        npscales = scales.non_position_scales()
        if len(npscales):
            data = [npscales.train_df(d) for d in data]
            data = [npscales.map_df(d) for d in data]

        # Train coordinate system
        panel.train_ranges(plot.coordinates)
        return data, panel, plot

    def draw_legend(self, plot):
        legend_box = plot.guides.build(plot)
        if not legend_box:
            return plot

        position = plot.theme._params['legend_position']
        # At what point (e.g [.94, .5]) on the figure
        # to place which point (e.g 6, for center left) of
        # the legend box
        lookup = {
            'right':  (6, (0.92, 0.5)),  # center left
            'left': (7, (0.07, 0.5)),    # center right
            'top': (8, (0.5, 0.92)),     # bottom center
            'bottom': (9, (0.5, 0.07))   # upper center
        }
        loc, box_to_anchor = lookup[position]
        anchored_box = AnchoredOffsetbox(
            loc=loc,
            child=legend_box,
            pad=0.,
            frameon=False,
            # Spacing goes here
            bbox_to_anchor=box_to_anchor,
            bbox_transform=plot.figure.transFigure,
            borderpad=0.,
        )
        plot.figure._themeable['legend_background'] = anchored_box
        ax = plot.axs[0]
        ax.add_artist(anchored_box)
        return plot


def set_breaks_and_labels(plot, ranges, finfo, ax):
    """
    Set the limits, breaks and labels for the axis

    Parameters
    ----------
    plot : ggplot
        ggplot object
    ranges : dict-like
        range information for the axes
    finfo : dict-like
        facet layout information
    ax : axes
        current axes
    """
    # It starts out with axes and labels on
    # all sides, we keep what we want and
    # get rid of what we don't want depending
    # on the plot

    # limits
    ax.set_xlim(ranges['x_range'])
    ax.set_ylim(ranges['y_range'])

    # breaks and labels for when the user set
    # them explicitly
    xbreaks = ranges['x_major']
    ybreaks = ranges['y_major']
    xlabels = ranges['x_labels']
    ylabels = ranges['y_labels']

    if not is_waive(xbreaks):
        ax.set_xticks(xbreaks)

    if not is_waive(ybreaks):
        ax.set_yticks(ybreaks)

    if not is_waive(xlabels):
        ax.set_xticklabels(xlabels)

    if not is_waive(ylabels):
        ax.set_yticklabels(ylabels)

    # Add axis Locators and Formatters for when
    # the mpl deals with the breaks and labels
    pscales = plot.scales.position_scales()
    for sc in pscales:
        with suppress(AttributeError):
            sc.trans.modify_axis(ax)

    bottomrow = finfo['ROW'] == plot.facet.nrow
    leftcol = finfo['COL'] == 1

    # Remove unwanted
    if isinstance(plot.facet, facet_wrap):
        if not finfo['AXIS_X']:
            ax.xaxis.set_ticks_position('none')
            ax.xaxis.set_ticklabels([])
        if not finfo['AXIS_Y']:
            ax.yaxis.set_ticks_position('none')
            ax.yaxis.set_ticklabels([])
        if finfo['AXIS_X']:
            ax.xaxis.set_ticks_position('bottom')
        if finfo['AXIS_Y']:
            ax.yaxis.set_ticks_position('left')
    else:
        if bottomrow:
            ax.xaxis.set_ticks_position('bottom')
        else:
            ax.xaxis.set_ticks_position('none')
            ax.xaxis.set_ticklabels([])

        if leftcol:
            ax.yaxis.set_ticks_position('left')
        else:
            ax.yaxis.set_ticks_position('none')
            ax.yaxis.set_ticklabels([])


def add_labels_and_title(plot):
    fig = plot.figure
    xlabel = plot.labels.get('x', '')
    ylabel = plot.labels.get('y', '')
    title = plot.labels.get('title', '')

    d = dict(
        axis_title_x=fig.text(0.5, 0.08, xlabel,
                              ha='center', va='top'),
        axis_title_y=fig.text(0.09, 0.5, ylabel,
                              ha='right', va='center',
                              rotation='vertical'),
        plot_title=fig.text(0.5, 0.92, title,
                            ha='center', va='bottom'))

    fig._themeable.update(d)


# TODO Need to use theme (element_rect) for the colors
def draw_facet_label(plot, finfo, ax):
    """
    Draw facet label onto the axes.

    This function will only draw labels if they
    are needed.

    Parameters
    ----------
    plot : ggplot
        ggplot object
    finfo : dict-like
        facet information
    ax : axes
        current axes
    fig : Figure
        current figure
    """
    fcwrap = isinstance(plot.facet, facet_wrap)
    fcgrid = isinstance(plot.facet, facet_grid)
    toprow = finfo['ROW'] == 1
    rightcol = finfo['COL'] == plot.facet.ncol

    if fcgrid and not toprow and not rightcol:
        return

    # The facet labels are placed onto the figure using
    # transAxes dimensions. The line height and line
    # width are mapped to the same [0, 1] range
    # i.e (pts) * (inches / pts) * (1 / inches)
    # plus a padding factor of 1.6
    bbox = ax.get_window_extent().transformed(
        plot.figure.dpi_scale_trans.inverted())
    w, h = bbox.width, bbox.height  # in inches

    fs = float(plot.theme._rcParams['font.size'])

    # linewidth in transAxes
    lwy = fs / (72*h)
    lwx = fs / (72*w)

    # bbox height (along direction of text) of
    # labels in transAxes
    hy = 1.6 * lwy
    hx = 1.6 * lwx

    # text location in transAxes
    y = 1 + hy/2.4
    x = 1 + hx/2.4

    d = plot.figure._themeable
    for key in ('strip_text_x', 'strip_text_y'):
        if key not in d:
            d[key] = []

    # facet_wrap #
    if fcwrap:
        # top label
        facet_var = plot.facet.vars[0]
        text = ax.text(0.5, y, finfo[facet_var],
                       bbox=dict(
                           xy=(0, 1),
                           facecolor='lightgrey',
                           edgecolor='lightgrey',
                           height=hy,
                           width=1,
                           transform=ax.transAxes),
                       transform=ax.transAxes,
                       fontdict=dict(verticalalignment='center',
                                     horizontalalignment='center'))
        d['strip_text_x'].append(text)

    # facet_grid #
    if fcgrid and toprow:
        # top labels
        facet_var = plot.facet.cols[0]
        text = ax.text(0.5, y, finfo[facet_var],
                       bbox=dict(
                           xy=(0, 1),
                           facecolor='lightgrey',
                           edgecolor='lightgrey',
                           height=hy,
                           width=1,
                           transform=ax.transAxes),
                       transform=ax.transAxes,
                       fontdict=dict(verticalalignment='center',
                                     horizontalalignment='center'))
        d['strip_text_x'].append(text)

    if fcgrid and rightcol and len(plot.facet.rows):
        # right labels
        facet_var = plot.facet.rows[0]
        text = ax.text(x, 0.5, finfo[facet_var],
                       bbox=dict(
                           xy=(1, 0),
                           facecolor='lightgrey',
                           edgecolor='lightgrey',
                           height=1,
                           width=hx,
                           transform=ax.transAxes),
                       transform=ax.transAxes,
                       fontdict=dict(rotation=-90,
                                     verticalalignment='center',
                                     horizontalalignment='center'))
        d['strip_text_y'].append(text)


def apply_facet_spacing(plot):
    # TODO: spaces should depend on the axis horizontal
    # and vertical lengths since the values are in
    # transAxes dimensions
    if isinstance(plot.facet, facet_wrap):
        plt.subplots_adjust(wspace=.05, hspace=.20)
    else:
        plt.subplots_adjust(wspace=.05, hspace=.05)
