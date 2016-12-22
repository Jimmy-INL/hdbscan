# Support various prediction methods for predicting cluster membership
# of new or unseen points. There are several ways to interpret how
# to do this correctly, so we provide several methods for
# the different use cases that may arise.

import numpy as np

from sklearn.neighbors import KDTree, BallTree
from ._prediction_utils import get_tree_row_with_child


class PredictionData(object):
    """
    Extra data that allows for faster prediction if cached.

    Parameters
    ----------

    data : array (n_samples, n_features)
        The original data set that was clustered

    condensed_tree : CondensedTree
        The condensed tree object created by a clustering

    min_samples : int
        The min_samples value used in clustering

    tree_type : string, optional
        Which type of space tree to use for core distance computation.
        One of:
            * ``kdtree``
            * ``balltree``

    metric : string, optional
        The metric used to determine distance for the clustering.
        This is the metric that will be used for the space tree to determine
        core distances etc.

    **kwargs :
        Any further arguments to the metric.

    Attributes
    ----------

    raw_data : array (n_samples, n_features)
        The original data set that was clustered

    tree : KDTree or BallTree
        A space partitioning tree that can be queried for nearest neighbors.

    core_distances : array (n_samples,)
        The core distances for every point in the original data set.

    cluster_map : dict
        A dictionary mapping cluster numbers in the condensed tree to labels
        in the final selected clustering.

    cluster_tree : structured array
        A version of the condensed tree that only contains clusters, not
        individual points.

    max_lambdas : dict
        A dictionary mapping cluster numbers in the condensed tree to the
        maximum lambda value seen in that cluster.
    """
    _tree_type_map = {'kdtree': KDTree, 'balltree': BallTree}

    def _clusters_below(self, cluster):
        result = []
        to_process = [cluster]

        while to_process:
            result.extend(to_process)
            to_process = \
                self.cluster_tree['child'][np.in1d(self.cluster_tree['parent'],
                                                   to_process)]
            to_process = to_process.tolist()

        return result

    def __init__(self, data, condensed_tree, min_samples,
                 tree_type='kdtree', metric='euclidean', **kwargs):
        self.raw_data = data
        self.tree = self._tree_type_map[tree_type](self.raw_data,
                                                   metric=metric, **kwargs)
        self.core_distances = self.tree.query(data, k=min_samples)[0][:, -1]

        selected_clusters = condensed_tree._select_clusters()
        raw_condensed_tree = condensed_tree.to_numpy()

        self.cluster_map = dict(zip(selected_clusters,
                                    range(len(selected_clusters))))
        self.cluster_tree = raw_condensed_tree[raw_condensed_tree['child_size']
                                               > 1]
        self.max_lambdas = {}
        self.leaf_max_lambdas = {}

        for cluster in set(self.cluster_tree[:,:2].flatten()):
            self.leaf_max_lambdas[cluster] = raw_condensed_tree[
                    raw_condensed_tree['parent'] == cluster].max()

        for cluster in selected_clusters:
            self.max_lambdas[cluster] = \
                raw_condensed_tree['lambda_val'][raw_condensed_tree['parent']
                                                 == cluster].max()

            for sub_cluster in self._clusters_below(cluster):
                self.cluster_map[sub_cluster] = self.cluster_map[cluster]
                self.max_lambdas[sub_cluster] = self.max_lambdas[cluster]

                exemplar_points = raw_condensed_tree[raw_condensed_tree[
                    'lambda_val'] == self.leaf_max_lambdas[cluster]]
                exemplars[cluster].extend(exemplar_points)

        self.exemplars = [np.array(x) for x in exemplars.values()]


def _find_neighbor_and_lambda(neighbor_indices, neighbor_distances,
                              core_distances, min_samples):
    """
    Find the nearest mutual reachability neighbor of a point, and  compute
    the associated lambda value for the point, given the mutual reachability
    distance to a nearest neighbor.

    Parameters
    ----------
    neighbor_indices : array (2 * min_samples, )
        An array of raw distance based nearest neighbor indices.

    neighbor_distances : array (2 * min_samples, )
        An array of raw distances to the nearest neighbors.

    core_distances : array (n_samples, )
        An array of core distances for all points

    min_samples : int
        The min_samples value used to generate core distances.

    Returns
    -------
    neighbor : int
        The index into the full raw data set of the nearest mutual reachability
        distance neighbor of the point.

    lambda_ : float
        The lambda value at which this point joins/merges with `neighbor`.
    """
    neighbor_core_distances = core_distances[neighbor_indices]
    point_core_distances = neighbor_distances[min_samples] * np.ones(
        neighbor_indices.shape[0])
    mr_distances = np.vstack((
        neighbor_core_distances,
        point_core_distances,
        neighbor_distances
    )).max(axis=0)

    nn_index = mr_distances.argmin()

    nearest_neighbor = neighbor_indices[nn_index]
    lambda_ = 1. / mr_distances[nn_index]

    return nearest_neighbor, lambda_


def _extend_condensed_tree(tree, neighbor_indices, neighbor_distances,
                           core_distances, min_samples):
    """
    Create a new condensed tree with an additional point added, allowing for
    computations as if this point had been part of the original tree. Note
    that this makes as little change to the tree as possible, with no
    re-optimizing/re-condensing so that the selected clusters remain
    effectively unchanged.

    Parameters
    ----------
    tree : structured array
        The raw format condensed tree to update.

    neighbor_indices : array (2 * min_samples, )
        An array of raw distance based nearest neighbor indices.

    neighbor_distances : array (2 * min_samples, )
        An array of raw distances to the nearest neighbors.

    core_distances : array (n_samples, )
        An array of core distances for all points

    min_samples : int
        The min_samples value used to generate core distances.

    Returns
    -------
    new_tree : structured array
        The original tree with an extra row providing the parent cluster
        and lambda information for a new point given index -1.
    """
    tree_root = tree['parent'].min()

    nearest_neighbor, lambda_ = _find_neighbor_and_lambda(neighbor_indices,
                                                          neighbor_distances,
                                                          core_distances,
                                                          min_samples
                                                          )

    neighbor_tree_row = get_tree_row_with_child(tree, nearest_neighbor)
    potential_cluster = neighbor_tree_row['parent']

    if neighbor_tree_row['lambda_val'] <= lambda_:
        # New point departs with the old
        new_tree_row = (potential_cluster, -1, 1,
                        neighbor_tree_row['lambda_val'])
    else:
        # Find appropriate cluster based on lambda of new point
        while potential_cluster > tree_root and \
                        tree[tree['child'] ==
                                potential_cluster]['lambda_val'] >= lambda_:
            potential_cluster = tree['parent'][tree['child']
                                               == potential_cluster][0]

        new_tree_row = (potential_cluster, -1, 1, lambda_)

    return np.append(tree, new_tree_row)


def _find_cluster_and_probability(tree, cluster_tree, neighbor_indices,
                                  neighbor_distances, core_distances,
                                  cluster_map, max_lambdas,
                                  min_samples):
    """
    Return the cluster label (of the original clustering) and membership
    probability of a new data point.

    Parameters
    ----------
    tree : CondensedTree
        The condensed tree associated with the clustering.

    cluster_tree : structured_array
        The raw form of the condensed tree with only cluster information (no
        data on individual points). This is significantly more compact.

    neighbor_indices : array (2 * min_samples, )
        An array of raw distance based nearest neighbor indices.

    neighbor_distances : array (2 * min_samples, )
        An array of raw distances to the nearest neighbors.

    core_distances : array (n_samples, )
        An array of core distances for all points

    cluster_map : dict
        A dictionary mapping cluster numbers in the condensed tree to labels
        in the final selected clustering.

    max_lambdas : dict
        A dictionary mapping cluster numbers in the condensed tree to the
        maximum lambda value seen in that cluster.

    min_samples : int
        The min_samples value used to generate core distances.
    """
    raw_tree = tree._raw_tree
    tree_root = cluster_tree['parent'].min()

    nearest_neighbor, lambda_ = _find_neighbor_and_lambda(neighbor_indices,
                                                          neighbor_distances,
                                                          core_distances,
                                                          min_samples
                                                          )

    neighbor_tree_row = get_tree_row_with_child(raw_tree, nearest_neighbor)
    potential_cluster = neighbor_tree_row['parent']

    if neighbor_tree_row['lambda_val'] > lambda_:
        # Find appropriate cluster based on lambda of new point
        while potential_cluster > tree_root and \
                        cluster_tree['lambda_val'][cluster_tree['child']
                                == potential_cluster] >= lambda_:
            potential_cluster = cluster_tree['parent'][cluster_tree['child']
                                                       == potential_cluster][0]

    if potential_cluster in cluster_map:
        cluster_label = cluster_map[potential_cluster]
    else:
        cluster_label = -1

    if cluster_label >= 0:
        max_lambda = max_lambdas[potential_cluster]

        if max_lambda > 0.0:
            lambda_ = min(max_lambda, lambda_)
            prob = (lambda_ / max_lambda)
        else:
            prob = 1.0
    else:
        prob = 0.0

    return cluster_label, prob


def approximate_predict(clusterer, points_to_predict):
    """Predict the cluster label of new points. The returned labels
    will be those of the original clustering found by ``clustererer``,
    and therefore are not (necessarily) the cluster labels that would
    be found by clustering the original data combined with
    ``points_to_predict``, hence the 'approximate' label.

    If you simply wish to assign new points to an existing clustering
    in the 'best' way possible, this is the function to use. If you
    want to predict how ``points_to_predict`` would cluster with
    the original data under HDBSCAN, you want ``reclustering_predict``.

    Parameters
    ----------
    clusterer : HDBSCAN
        A clustering object that has been fit to the data and
        either had ``prediction_data=True`` set, or called the
        ``generate_prediction_data`` method after the fact.

    points_to_predict : array, or array-like (n_samples, n_features)
        The new data points to predict cluster labels for. They should
        have the same dimensionality as the original dataset over which
        clusterer was fit.

    Returns
    -------
    labels : array (n_samples,)
        The predicted labels of the ``points_to_predict``

    probabilities : array (n_samples,)
        The soft cluster scores for each of the ``points_to_predict``

    See Also
    --------
    ``reclustering_predict``
    ``membership_vector``

    """
    if clusterer.prediction_data_ is None:
        raise ValueError('Clusterer does not have prediction data!'
                         ' Try fitting with prediction_data=True set,'
                         ' or run generate_preiction_data on the clusterer')

    points_to_predict = np.asarray(points_to_predict)

    if points_to_predict.shape[1] != \
            clusterer.prediction_data_.raw_data.shape[1]:
        raise ValueError('New points dimension does not match fit data!')

    labels = np.empty(points_to_predict.shape[0], dtype=np.int)
    probabilities = np.empty(points_to_predict.shape[0], dtype=np.float64)

    min_samples = clusterer.min_samples or clusterer.min_cluster_size
    neighbor_distances, neighbor_indices = \
        clusterer.prediction_data_.tree.query(points_to_predict,
                                              k=2 * min_samples)

    for i in range(points_to_predict.shape[0]):
        label, prob = _find_cluster_and_probability(
            clusterer.condensed_tree_,
            clusterer.prediction_data_.cluster_tree,
            neighbor_indices[i],
            neighbor_distances[i],
            clusterer.prediction_data_.core_distances,
            clusterer.prediction_data_.cluster_map,
            clusterer.prediction_data_.max_lambdas,
            min_samples
        )
        labels[i] = label
        probabilities[i] = prob

    return labels, probabilities

def reclustering_predict(clusterer, points_to_predict):
    pass


def membership_vector(clusterer, points_to_predict):
    pass