import numpy as np
import typing

from defenses.prevention.MedianFilteringDefense import MedianFilteringDefense
from attack.adaptive_attack.AdaptiveAttack import AdaptiveAttackOnAttackImage
from scaling.ScalingApproach import ScalingApproach

from attack.adaptive_attack.cythmodule.adaptivemedianfiltering import adaptive_attack_median_filtering_cython


class AdaptiveMedianAttack(AdaptiveAttackOnAttackImage):
    """
    Adaptive attack from paper to mislead median-filter-based defense (defense 2 in paper).
    """

    def __init__(self,
                 verbose: bool,
                 scaler_approach: ScalingApproach,
                 medianfilteringdefense: MedianFilteringDefense,
                 choose_only_unused_pixels_in_overlapping_case: bool,
                 allowed_ratio_of_change: float,
                 usecython: bool
                 ):
        """

        :param verbose: print debug messages
        :param scaler_approach: scaler approach
        :param medianfilteringdefense: MedianFilteringDefense
        :param choose_only_unused_pixels_in_overlapping_case: if true, we try to change only pixels
        that were not changed before. if false, we ignore previously set values in overlapping windows.
        Preliminary results show that false leads to better results visually..
        :param allowed_ratio_of_change: in %, the number of pixels that can be changed.
        :param usecython: use cython-based attack..
        """
        super().__init__(verbose, scaler_approach)
        self.medianfilteringfefense = medianfilteringdefense
        self.eps = 3

        self.choose_only_unused_pixels_in_overlapping_case = choose_only_unused_pixels_in_overlapping_case
        self.allowed_ratio_of_change: float = allowed_ratio_of_change
        self.usecython = usecython

        self.last_run_changed_pixels: typing.List[typing.List[float]] = []
        self.last_run_nosuccess: typing.List[float] = []

        # self.last_run_l2dist: typing.List[typing.List[float]] = []

        if self.choose_only_unused_pixels_in_overlapping_case == True and self.usecython == True:
            raise Exception("choose_only_unused_pixels_in_overlapping_case not implemented in cython, yet")



    def get_stats_last_run(self) -> typing.Tuple[typing.List[typing.List[float]] , typing.List[float]]:
        return self.last_run_changed_pixels, self.last_run_nosuccess



    # @Overwrite
    def counter_attack(self, att_image: np.ndarray) -> np.ndarray:

        # I. get binary mask
        # todo a cleaner way would be to use the binary mask from medianfilteringdefense. make class stateful then.
        dir_attack_image = self.medianfilteringfefense.fourierpeakmatrixcollector.get(
            scaler_approach=self.medianfilteringfefense.scaler_approach)
        binary_mask_indices = np.where(dir_attack_image != 255)
        binary_mask = np.zeros((self.medianfilteringfefense.scaler_approach.cl_matrix.shape[1],
                                self.medianfilteringfefense.scaler_approach.cr_matrix.shape[0]))
        binary_mask[binary_mask_indices] = 1

        # II. go over each channel if necessary
        if len(att_image.shape) == 2:
            if not self.usecython:
                r = self.__apply_attack(att_image=att_image, binary_mask=binary_mask)
            else:
                r = self.__apply_attack_cython(att_image=att_image, binary_mask=binary_mask)
            return r.astype(np.uint8)
        else:
            filtered_att_image = np.zeros(att_image.shape)
            for ch in range(att_image.shape[2]):
                if self.verbose is True:
                    print("Channel:", ch)

                if not self.usecython:
                    re = self.__apply_attack(att_image=att_image[:, :, ch], binary_mask=binary_mask)
                else:
                    re = self.__apply_attack_cython(att_image=att_image[:, :, ch], binary_mask=binary_mask)

                filtered_att_image[:, :, ch] = re
            return filtered_att_image.astype(np.uint8)


    def __apply_attack(self, att_image, binary_mask):
        filtered_attack_image = np.copy(att_image)
        positions = np.where(binary_mask == 1)

        # we convert to float for inserting nans, then we insert nan at all locations that are marked in binary-mask.
        #   later, when we compute the median around each marked location, we can very simply ignore all other
        #   marked locations that are inside the window
        base_attack_image = np.copy(att_image)
        base_attack_image = base_attack_image.astype('float64')
        assert np.any(np.isnan(base_attack_image)) == False
        base_attack_image[positions] = np.nan
        base_marked_attack_image = base_attack_image.copy()

        # apply median filter
        xpos = positions[0]
        ypos = positions[1]

        no_success: int = 0 # counter for non-successful windows
        l0_changes = [] # count the number of changed pixels per window
        for pix_src_r, pix_src_c in zip(xpos, ypos):
            target_value = att_image[pix_src_r, pix_src_c] # the median around current considered pixel should lead to..

            # get the block
            ix_l = max(0, pix_src_r - self.medianfilteringfefense.bandwidth[0])
            ix_r = min(pix_src_r + self.medianfilteringfefense.bandwidth[0] + 1, filtered_attack_image.shape[0])
            jx_u = max(0, pix_src_c - self.medianfilteringfefense.bandwidth[1])
            jx_b = min(pix_src_c + self.medianfilteringfefense.bandwidth[1] + 1, filtered_attack_image.shape[1])

            cur_block = base_attack_image[ix_l:ix_r, jx_u:jx_b]  # a view; changes base_attack_image as well.
            cur_block_result = filtered_attack_image[ix_l:ix_r, jx_u:jx_b]
            cur_block_marked = base_marked_attack_image[ix_l:ix_r, jx_u:jx_b]
            # cur_median = np.nanmedian(cur_block)
            cur_median = MedianFilteringDefense.get_median_nan(cur_block)


            if np.abs(target_value - cur_median) < self.eps:
                continue

            increase = bool(target_value > cur_median)

            block_pixels = AdaptiveMedianAttack.take_closest_values(
                cur_block=cur_block, increase=increase, target_value=target_value)
            # block_pixels = AdaptiveMedianAttack.iterate_top_bottom(
            #     cur_block=cur_block, increase=increase, target_value=target_value)

            success: bool = False
            changes: int = 0
            if self.choose_only_unused_pixels_in_overlapping_case is True:
                possible_changes = np.sum(~np.isnan(cur_block_marked))
            else:
                possible_changes = np.sum(~np.isnan(cur_block))

            for block_r, block_c in block_pixels:
                na_med = MedianFilteringDefense.get_median_nan(cur_block)
                if increase is True and na_med >= target_value:
                    # if np.nanmedian(cur_block) > target_value:
                    #     print(np.nanmedian(cur_block), target_value)
                    success = True
                    break
                elif increase is False and na_med <= target_value:
                    # if np.nanmedian(cur_block) < target_value:
                    #     print(np.nanmedian(cur_block), target_value)
                    success = True
                    break

                if (changes / possible_changes) >= self.allowed_ratio_of_change:
                    break

                if not np.isnan(cur_block[block_r, block_c]):
                    if self.choose_only_unused_pixels_in_overlapping_case is False or\
                            not np.isnan(cur_block_marked[block_r, block_c]):
                        cur_block[block_r, block_c] = target_value
                        cur_block_result[block_r, block_c] = target_value
                        cur_block_marked[block_r, block_c] = np.nan
                        changes += 1

            assert changes <= possible_changes
            if success is False:
                no_success += 1

            l0_changes.append(changes/possible_changes)


        # adv_changed = np.where( (np.isnan(base_marked_attack_image) & (binary_mask != 1)) )
        # marked_adv_percentage = len(adv_changed[0])/(base_marked_attack_image.shape[0]*base_marked_attack_image.shape[1])
        if self.verbose:
            # print("Marked:", marked_adv_percentage)
            print("No success: {} ({}%)".format(no_success, no_success / len(xpos)))

        self.last_run_nosuccess.append(no_success/len(xpos))
        self.last_run_changed_pixels.append(l0_changes)

        return filtered_attack_image


    @staticmethod
    def take_closest_values(cur_block: np.ndarray, increase: bool, target_value: int) \
            -> typing.List[typing.Tuple[int, int]]:
        # S2: choose the largest pixels that are smaller than target value (increase block median)
        # S2: choose the smallest pixels that are larger than target value (decrease block median)

        if increase:
            sorted_indices_block = np.dstack(np.unravel_index(np.argsort(cur_block.ravel()), cur_block.shape))[0][::-1]
        else:
            sorted_indices_block = np.dstack(np.unravel_index(np.argsort(cur_block.ravel()), cur_block.shape))[0]

        block_pixels = []

        for cur_index in sorted_indices_block:
            if not np.isnan(cur_block[cur_index[0], cur_index[1]]):
                if increase is True and cur_block[cur_index[0], cur_index[1]] < target_value:
                    block_pixels.append((cur_index[0], cur_index[1]))
                elif increase is False and cur_block[cur_index[0], cur_index[1]] > target_value:
                    block_pixels.append((cur_index[0], cur_index[1]))
        return block_pixels


    def __apply_attack_cython(self, att_image: np.ndarray, binary_mask: np.ndarray) -> np.ndarray:
        """
        This is a cython wrapper that calls the respective cython function. Much faster than the Python version.
        :param att_image: image under investigation
        :param binary_mask: binary mask: pixels that are considered
        :return: filtered image
        """

        filtered_attack_image = np.copy(att_image)
        positions = np.where(binary_mask == 1)
        xpos = positions[0]
        ypos = positions[1]

        res, l0_changes, no_success_score = adaptive_attack_median_filtering_cython(att_image, filtered_attack_image,
                                      binary_mask.astype(np.uint8), xpos, ypos,
                                      self.medianfilteringfefense.bandwidth[0],
                                      self.medianfilteringfefense.bandwidth[1], self.eps,
                                                                  self.allowed_ratio_of_change)

        self.last_run_changed_pixels.append(l0_changes)
        self.last_run_nosuccess.append(no_success_score)

        if self.verbose:
            # print("Marked:", marked_adv_percentage)
            print("No success: {}%".format(no_success_score))

        return np.array(res) # cython returns memoryview..

